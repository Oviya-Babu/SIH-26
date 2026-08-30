"""Internal MediKiosk consent (CLAUDE.md §7.2).

This module owns MediKiosk's OWN consent: purpose-specific, audio-explained,
revocable, and required for every session, always. It gates everything MediKiosk
itself does — voice capture, document processing, AI processing, staff access.

[RED LINE §7.2] It is NOT the ABDM network consent artifact. That artifact is
owned by a registered Consent Manager and lives in a structurally distinct table
(:mod:`medikiosk.modules.integration.abdm`). Nothing here may be presented as an
ABDM consent, and nothing there may gate internal processing.

[RED LINE §6] A caregiver may grant consent ONLY under documented authority. The
default basis (``patient_present_and_acknowledges``) makes them a respondent, not
a grantor — enforced here, in the database trigger, and in the RBAC capability
set, so no single layer carries the rule alone.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

import asyncpg

from medikiosk.db import Principal
from medikiosk.errors import ConsentRequired, Forbidden, NotFound, ValidationFailed
from medikiosk.modules.audit import service as audit


class Purpose(StrEnum):
    VOICE_CAPTURE = "voice_capture"
    DOCUMENT_PROCESSING = "document_processing"
    AI_PROCESSING = "ai_processing"
    STAFF_ACCESS = "staff_access"
    ABDM_SHARING_INTENT = "abdm_sharing_intent"


# Only staff access is mandatory: without it nothing can reach the physician, so
# the kiosk pathway is pointless. Everything else is genuinely optional, and the
# product must remain usable when a patient declines it (§7.2, §37).
REQUIRED_PURPOSES: frozenset[Purpose] = frozenset({Purpose.STAFF_ACCESS})

# Documented-authority bases under which a caregiver MAY grant consent (§6).
CONSENT_GRANTING_BASES: frozenset[str] = frozenset(
    {"documented_guardianship", "documented_medical_power_of_attorney"}
)


@dataclass(frozen=True, slots=True)
class ConsentGrant:
    purpose: Purpose
    granted: bool


@dataclass(frozen=True, slots=True)
class ConsentState:
    patient_id: UUID
    granted: frozenset[Purpose]
    notice_version: str | None

    def allows(self, purpose: Purpose) -> bool:
        return purpose in self.granted

    def require(self, purpose: Purpose) -> None:
        if purpose not in self.granted:
            raise ConsentRequired(
                f"consent for {purpose} has not been granted",
                reason_code="consent_required",
                detail={"purpose": str(purpose)},
            )


async def record_consents(
    conn: asyncpg.Connection,
    principal: Principal,
    *,
    patient_id: UUID,
    grants: list[ConsentGrant],
    notice_version: str,
    notice_language: str,
    audio_explained: bool,
    grantor_type: str,
    caregiver_auth_id: UUID | None = None,
) -> ConsentState:
    """Persist a consent decision set, in one transaction with its audit rows."""
    if not grants:
        raise ValidationFailed("no consent decisions supplied", reason_code="validation_failed")

    seen: set[Purpose] = set()
    for grant in grants:
        if grant.purpose in seen:
            raise ValidationFailed(
                f"duplicate decision for {grant.purpose}", reason_code="validation_failed"
            )
        seen.add(grant.purpose)

    missing_required = {p for p in REQUIRED_PURPOSES} - seen
    if missing_required:
        raise ValidationFailed(
            "a decision is required for: " + ", ".join(sorted(missing_required)),
            reason_code="validation_failed",
        )

    if grantor_type == "caregiver":
        await _assert_caregiver_may_grant(conn, patient_id, caregiver_auth_id)
    elif grantor_type == "patient":
        caregiver_auth_id = None
    else:
        raise ValidationFailed("unknown grantor type", reason_code="validation_failed")

    for grant in grants:
        # Re-granting supersedes: revoke the previous live row for the purpose,
        # then insert. History is preserved — a consent record is evidence.
        await conn.execute(
            """
            UPDATE consent
               SET revoked_at = now()
             WHERE patient_id = $1 AND purpose = $2 AND revoked_at IS NULL
            """,
            patient_id,
            str(grant.purpose),
        )
        consent_id = await conn.fetchval(
            """
            INSERT INTO consent (tenant_id, patient_id, purpose, granted, notice_version,
                                 notice_language, audio_explained, grantor_type,
                                 grantor_caregiver_auth_id)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            RETURNING id
            """,
            principal.tenant_id,
            patient_id,
            str(grant.purpose),
            grant.granted,
            notice_version,
            notice_language,
            audio_explained,
            grantor_type,
            caregiver_auth_id,
        )
        await audit.record(
            conn,
            principal,
            action="consent.recorded",
            entity_type="consent",
            entity_id=consent_id,
            detail={
                "purpose": str(grant.purpose),
                "granted": grant.granted,
                "notice_version": notice_version,
                "notice_language": notice_language,
                "grantor_type": grantor_type,
            },
        )

    return await current_state(conn, patient_id)


async def _assert_caregiver_may_grant(
    conn: asyncpg.Connection, patient_id: UUID, caregiver_auth_id: UUID | None
) -> None:
    if caregiver_auth_id is None:
        raise Forbidden(
            "caregiver-granted consent requires a documented authorization",
            reason_code="caregiver_cannot_grant_consent",
        )
    basis = await conn.fetchval(
        """
        SELECT authority_basis
          FROM caregiver_authorization
         WHERE id = $1 AND patient_id = $2 AND revoked_at IS NULL
        """,
        caregiver_auth_id,
        patient_id,
    )
    if basis is None:
        raise NotFound("caregiver authorization not found", reason_code="not_found")
    if basis not in CONSENT_GRANTING_BASES:
        # [RED LINE §6] Blood relationship or mere presence never grants
        # consent authority. The caregiver remains a respondent.
        raise Forbidden(
            "this caregiver may act as a respondent but may not grant consent",
            reason_code="caregiver_cannot_grant_consent",
        )


async def current_state(conn: asyncpg.Connection, patient_id: UUID) -> ConsentState:
    rows = await conn.fetch(
        """
        SELECT purpose, granted, notice_version
          FROM consent
         WHERE patient_id = $1 AND revoked_at IS NULL
        """,
        patient_id,
    )
    granted = {Purpose(r["purpose"]) for r in rows if r["granted"]}
    notice = next((r["notice_version"] for r in rows), None)
    return ConsentState(patient_id=patient_id, granted=frozenset(granted), notice_version=notice)


async def revoke(
    conn: asyncpg.Connection,
    principal: Principal,
    *,
    consent_id: UUID,
) -> ConsentState:
    """``DELETE /v1/consents/{id}`` (§7.2) — revocation, not deletion.

    The row is marked revoked rather than removed: a consent record is evidence
    of what was agreed and when, and destroying it would defeat the audit.
    """
    row = await conn.fetchrow(
        """
        UPDATE consent
           SET revoked_at = now()
         WHERE id = $1 AND revoked_at IS NULL
        RETURNING patient_id, purpose
        """,
        consent_id,
    )
    if row is None:
        raise NotFound("consent not found or already revoked", reason_code="not_found")

    await audit.record(
        conn,
        principal,
        action="consent.revoked",
        entity_type="consent",
        entity_id=consent_id,
        detail={"purpose": row["purpose"]},
    )
    return await current_state(conn, row["patient_id"])


async def revoke_purpose(
    conn: asyncpg.Connection,
    principal: Principal,
    *,
    patient_id: UUID,
    purpose: Purpose,
) -> ConsentState:
    rows = await conn.fetch(
        """
        UPDATE consent
           SET revoked_at = now()
         WHERE patient_id = $1 AND purpose = $2 AND revoked_at IS NULL
        RETURNING id
        """,
        patient_id,
        str(purpose),
    )
    if not rows:
        raise NotFound("no active consent for that purpose", reason_code="not_found")
    for r in rows:
        await audit.record(
            conn,
            principal,
            action="consent.revoked",
            entity_type="consent",
            entity_id=r["id"],
            detail={"purpose": str(purpose)},
        )
    return await current_state(conn, patient_id)


async def status_report(conn: asyncpg.Connection) -> list[dict]:
    """Tenant-wide consent posture, for the Security/Privacy Officer (§5.2).

    Aggregate only: the officer needs coverage figures, not who consented to
    what. Counting rather than listing is the data-minimisation choice.
    """
    rows = await conn.fetch(
        """
        SELECT purpose,
               count(*) FILTER (WHERE granted AND revoked_at IS NULL)      AS active_grants,
               count(*) FILTER (WHERE NOT granted AND revoked_at IS NULL)  AS active_refusals,
               count(*) FILTER (WHERE revoked_at IS NOT NULL)              AS revoked,
               count(*) FILTER (WHERE audio_explained)                     AS audio_explained,
               count(DISTINCT notice_version)                              AS notice_versions
          FROM consent
         GROUP BY purpose
         ORDER BY purpose
        """
    )
    return [dict(r) for r in rows]
