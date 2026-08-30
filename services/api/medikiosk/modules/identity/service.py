"""Patient identity — ABHA-first, local registration always available (§7.1).

[RED LINE §7.1] MediKiosk never stores a raw Aadhaar number, at any point, in
any field, in any log. Where Aadhaar e-KYC is involved it happens entirely inside
ABDM's own infrastructure via the ABHA-creation handoff; MediKiosk only ever
receives an ABHA reference back.

The local-registration path must never be blocked: a patient with no ABHA card
still gets seen today.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID

import asyncpg

from medikiosk.db import Principal
from medikiosk.errors import ValidationFailed
from medikiosk.modules.audit import service as audit
from medikiosk.modules.localization.registry import SUPPORTED_LANGUAGES

# Aadhaar is 12 digits beginning 2-9. We detect and REFUSE it rather than
# storing it, in any field a caller might try to smuggle it through.
_AADHAAR_SHAPED = re.compile(r"^\s*[2-9]\d{3}[\s-]?\d{4}[\s-]?\d{4}\s*$")
_DIGITS_ONLY = re.compile(r"^\d+$")


class AadhaarRefused(ValidationFailed):
    reason_code = "aadhaar_not_accepted"


def assert_not_aadhaar(*values: str | None) -> None:
    """Refuse anything Aadhaar-shaped before it can reach the database.

    This is the application half of the control; migration 0001 adds a CHECK
    constraint as the storage half, so neither layer relies on the other.
    """
    for value in values:
        if value and _AADHAAR_SHAPED.match(value):
            raise AadhaarRefused(
                "MediKiosk does not accept Aadhaar numbers (CLAUDE.md §7.1)",
                reason_code="aadhaar_not_accepted",
            )


@dataclass(frozen=True, slots=True)
class PatientRecord:
    id: UUID
    tenant_id: UUID
    abha_reference: str | None
    hospital_local_id: str | None
    full_name: str
    year_of_birth: int | None
    gender: str | None
    preferred_language: str
    is_new: bool


def _validate_name(name: str) -> str:
    cleaned = " ".join(name.split())
    if len(cleaned) < 2:
        raise ValidationFailed("name is too short", reason_code="name_too_short")
    if len(cleaned) > 120:
        raise ValidationFailed("name is too long", reason_code="name_too_long")
    if _DIGITS_ONLY.match(cleaned.replace(" ", "")):
        raise ValidationFailed("name cannot be only digits", reason_code="name_invalid")
    return cleaned


def _validate_year_of_birth(year: int | None) -> int | None:
    if year is None:
        return None
    current = datetime.now(timezone.utc).year
    if not (current - 130) <= year <= current:
        raise ValidationFailed("year of birth is out of range", reason_code="out_of_range")
    return year


def _validate_language(language: str) -> str:
    if language not in SUPPORTED_LANGUAGES:
        raise ValidationFailed(
            f"unsupported language: {language}", reason_code="unsupported_language"
        )
    return language


def _validate_phone_last4(value: str | None) -> str | None:
    """Only the last four digits are ever collected (data minimisation, §28)."""
    if value is None or value == "":
        return None
    cleaned = value.strip()
    if not (len(cleaned) == 4 and cleaned.isdigit()):
        raise ValidationFailed(
            "provide exactly the last 4 digits", reason_code="phone_last4_invalid"
        )
    return cleaned


async def register_local(
    conn: asyncpg.Connection,
    principal: Principal,
    *,
    hospital_local_id: str | None,
    full_name: str,
    year_of_birth: int | None,
    gender: str | None,
    phone_last4: str | None,
    preferred_language: str,
) -> PatientRecord:
    """Local hospital registration — the always-available path (§7.1)."""
    assert_not_aadhaar(hospital_local_id, full_name, phone_last4)
    name = _validate_name(full_name)
    year = _validate_year_of_birth(year_of_birth)
    language = _validate_language(preferred_language)
    last4 = _validate_phone_last4(phone_last4)

    if gender is not None and gender not in ("male", "female", "other", "undisclosed"):
        raise ValidationFailed("unknown gender value", reason_code="gender_invalid")

    local_id = (hospital_local_id or "").strip() or None
    if local_id is None:
        # Generate a hospital-scoped id so the patient is identifiable within the
        # tenant without any national identifier at all.
        local_id = await _next_local_id(conn, principal.tenant_id)

    existing = await conn.fetchrow(
        "SELECT * FROM patient WHERE hospital_local_id = $1", local_id
    )
    if existing is not None:
        return _to_record(existing, is_new=False)

    row = await conn.fetchrow(
        """
        INSERT INTO patient (tenant_id, hospital_local_id, full_name, year_of_birth,
                             gender, phone_last4, preferred_language)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        RETURNING *
        """,
        principal.tenant_id,
        local_id,
        name,
        year,
        gender,
        last4,
        language,
    )
    await audit.record(
        conn,
        principal,
        action="patient.registered_locally",
        entity_type="patient",
        entity_id=row["id"],
        detail={"language": language, "reason_code": "local_registration"},
    )
    return _to_record(row, is_new=True)


async def upsert_from_abha(
    conn: asyncpg.Connection,
    principal: Principal,
    *,
    abha_reference: str,
    full_name: str,
    year_of_birth: int | None,
    gender: str | None,
    preferred_language: str,
) -> PatientRecord:
    """Record a patient identified by an ABHA reference returned by ABDM.

    Only the reference is stored — never Aadhaar, and never the ABHA token used
    to obtain it.
    """
    assert_not_aadhaar(abha_reference, full_name)
    reference = abha_reference.strip()
    if not reference or len(reference) > 128:
        raise ValidationFailed("invalid ABHA reference", reason_code="abha_reference_invalid")

    name = _validate_name(full_name)
    year = _validate_year_of_birth(year_of_birth)
    language = _validate_language(preferred_language)

    existing = await conn.fetchrow(
        "SELECT * FROM patient WHERE abha_reference = $1", reference
    )
    if existing is not None:
        await conn.execute(
            "UPDATE patient SET preferred_language = $2, updated_at = now() WHERE id = $1",
            existing["id"],
            language,
        )
        return _to_record({**dict(existing), "preferred_language": language}, is_new=False)

    row = await conn.fetchrow(
        """
        INSERT INTO patient (tenant_id, abha_reference, full_name, year_of_birth,
                             gender, preferred_language)
        VALUES ($1, $2, $3, $4, $5, $6)
        RETURNING *
        """,
        principal.tenant_id,
        reference,
        name,
        year,
        gender,
        language,
    )
    await audit.record(
        conn,
        principal,
        action="patient.identified_by_abha",
        entity_type="patient",
        entity_id=row["id"],
        detail={"language": language, "environment": "sandbox"},
    )
    return _to_record(row, is_new=True)


async def get_patient(conn: asyncpg.Connection, patient_id: UUID) -> PatientRecord | None:
    row = await conn.fetchrow("SELECT * FROM patient WHERE id = $1", patient_id)
    return _to_record(row, is_new=False) if row else None


async def set_language(
    conn: asyncpg.Connection, patient_id: UUID, language: str
) -> None:
    await conn.execute(
        "UPDATE patient SET preferred_language = $2, updated_at = now() WHERE id = $1",
        patient_id,
        _validate_language(language),
    )


async def _next_local_id(conn: asyncpg.Connection, tenant_id: UUID) -> str:
    """Sequential, tenant-scoped, non-guessable-across-tenants local id."""
    count = await conn.fetchval("SELECT count(*) FROM patient") or 0
    year = datetime.now(timezone.utc).strftime("%Y")
    candidate = f"MK-{year}-{int(count) + 1:06d}"
    while await conn.fetchval(
        "SELECT 1 FROM patient WHERE hospital_local_id = $1", candidate
    ):
        count += 1
        candidate = f"MK-{year}-{int(count) + 1:06d}"
    return candidate


def _to_record(row, *, is_new: bool) -> PatientRecord:
    data = dict(row)
    return PatientRecord(
        id=data["id"],
        tenant_id=data["tenant_id"],
        abha_reference=data.get("abha_reference"),
        hospital_local_id=data.get("hospital_local_id"),
        full_name=data["full_name"],
        year_of_birth=data.get("year_of_birth"),
        gender=data.get("gender"),
        preferred_language=data.get("preferred_language", "en"),
        is_new=is_new,
    )
