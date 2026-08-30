"""Clinical Facts — the only writer to ``clinical_fact`` (CLAUDE.md §13, §20).

[RED LINE §20] AI never writes here. The AI Gateway has no database client and no
network route to PostgreSQL; extracted or suggested values reach this module only
as *validated input* from the orchestrating backend, and this module decides what
is persisted.

[RED LINE §13] ``patient_answer``, ``caregiver_answer``, ``document_extraction``
and ``physician_edit`` are never merged or silently overwritten. A correction
creates a NEW fact that supersedes the prior one, so the original remains
readable and attributable forever.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from uuid import UUID

import asyncpg

from medikiosk.db import Principal, to_jsonb
from medikiosk.errors import Conflict, NotFound, ValidationFailed
from medikiosk.modules.audit import service as audit


class SourceType(StrEnum):
    PATIENT_ANSWER = "patient_answer"
    CAREGIVER_ANSWER = "caregiver_answer"
    DOCUMENT_EXTRACTION = "document_extraction"
    STAFF_ENTRY = "staff_entry"
    PHYSICIAN_EDIT = "physician_edit"

    @property
    def requires_respondent(self) -> bool:
        """Only a document extraction may omit a respondent (§13).

        The uploader's identity is recorded on the document row instead, so the
        chain from fact to person is unbroken either way.
        """
        return self is not SourceType.DOCUMENT_EXTRACTION


class VerificationStatus(StrEnum):
    UNVERIFIED = "unverified"
    AWAITING_HUMAN_VERIFICATION = "awaiting_human_verification"
    PATIENT_CONFIRMED = "patient_confirmed"
    PHYSICIAN_VERIFIED = "physician_verified"
    PHYSICIAN_REJECTED = "physician_rejected"


@dataclass(slots=True)
class FactInput:
    session_id: UUID
    patient_id: UUID
    category: str
    concept_code: str
    concept_label: str
    value_normalized: Any
    confidence: float
    source_type: SourceType
    provenance_ref: dict[str, Any]
    value_raw: str | None = None
    unit: str | None = None
    respondent_id: UUID | None = None
    respondent_relationship: str | None = None
    verification_status: VerificationStatus = VerificationStatus.UNVERIFIED
    abnormal_flag: str | None = None
    supersedes: UUID | None = None
    extra_audit: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class FactRecord:
    id: UUID
    session_id: UUID
    category: str
    concept_code: str
    concept_label: str
    value_raw: str | None
    value_normalized: Any
    unit: str | None
    confidence: float
    source_type: str
    respondent_id: UUID | None
    respondent_relationship: str | None
    provenance_ref: dict[str, Any]
    verification_status: str
    is_conflicting: bool
    abnormal_flag: str | None
    superseded_by: UUID | None
    created_at: Any


def _validate(fact: FactInput) -> None:
    if not 0.0 <= fact.confidence <= 1.0:
        raise ValidationFailed("confidence must be within [0, 1]",
                               reason_code="validation_failed")
    if fact.source_type.requires_respondent and fact.respondent_id is None:
        # [RED LINE §6] no anonymous answers or uploads, ever.
        raise ValidationFailed(
            f"{fact.source_type} requires a respondent",
            reason_code="respondent_required",
        )
    if not fact.provenance_ref:
        raise ValidationFailed("provenance_ref must not be empty",
                               reason_code="provenance_required")
    required_provenance = {"method"}
    missing = required_provenance - set(fact.provenance_ref)
    if missing:
        raise ValidationFailed(
            "provenance_ref is missing: " + ", ".join(sorted(missing)),
            reason_code="provenance_incomplete",
        )


async def write(
    conn: asyncpg.Connection,
    principal: Principal,
    fact: FactInput,
) -> FactRecord:
    """Persist one clinical fact, with its audit row, in the caller's transaction.

    If ``supersedes`` is set the prior fact is linked rather than modified: its
    content columns are immutable at the database level (migration 0003).
    """
    _validate(fact)

    row = await conn.fetchrow(
        """
        INSERT INTO clinical_fact
            (tenant_id, session_id, patient_id, category, concept_code, concept_label,
             value_raw, value_normalized, unit, confidence, source_type,
             respondent_id, respondent_relationship, provenance_ref,
             verification_status, abnormal_flag)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9, $10, $11, $12, $13,
                $14::jsonb, $15, $16)
        RETURNING *
        """,
        principal.tenant_id,
        fact.session_id,
        fact.patient_id,
        fact.category,
        fact.concept_code,
        fact.concept_label,
        fact.value_raw,
        to_jsonb(fact.value_normalized),
        fact.unit,
        fact.confidence,
        str(fact.source_type),
        fact.respondent_id,
        fact.respondent_relationship,
        to_jsonb(fact.provenance_ref),
        str(fact.verification_status),
        fact.abnormal_flag,
    )

    if fact.supersedes is not None:
        updated = await conn.fetchval(
            """
            UPDATE clinical_fact
               SET superseded_by = $2
             WHERE id = $1 AND session_id = $3 AND superseded_by IS NULL
            RETURNING id
            """,
            fact.supersedes,
            row["id"],
            fact.session_id,
        )
        if updated is None:
            raise Conflict(
                "the fact being superseded is missing or already superseded",
                reason_code="conflict",
            )

    await audit.record(
        conn,
        principal,
        action="clinical_fact.written",
        entity_type="clinical_fact",
        entity_id=row["id"],
        detail={
            "category": fact.category,
            "concept_code": fact.concept_code,
            "respondent_type": _respondent_type(fact.source_type),
            "confidence_band": _band(fact.confidence),
            "superseded_fact_id": fact.supersedes,
            "abnormal_flag": fact.abnormal_flag,
            **fact.extra_audit,
        },
    )
    return _to_record(row)


async def supersede_with_physician_edit(
    conn: asyncpg.Connection,
    principal: Principal,
    *,
    fact_id: UUID,
    value_normalized: Any,
    value_raw: str | None,
    reason: str | None = None,
) -> FactRecord:
    """A physician correction (§13, §21).

    Creates a NEW fact of source ``physician_edit`` that references the prior
    one. The original is preserved and remains visible in the provenance trail —
    the physician's judgment is recorded as an act, not as a rewrite of what the
    patient said.
    """
    prior = await conn.fetchrow(
        "SELECT * FROM clinical_fact WHERE id = $1 AND superseded_by IS NULL", fact_id
    )
    if prior is None:
        raise NotFound("fact not found or already superseded", reason_code="not_found")

    fact = FactInput(
        session_id=prior["session_id"],
        patient_id=prior["patient_id"],
        category=prior["category"],
        concept_code=prior["concept_code"],
        concept_label=prior["concept_label"],
        value_normalized=value_normalized,
        value_raw=value_raw,
        unit=prior["unit"],
        confidence=1.0,  # a physician's own entry is not a probabilistic estimate
        source_type=SourceType.PHYSICIAN_EDIT,
        respondent_id=principal.actor_id,
        respondent_relationship=None,
        provenance_ref={
            "method": "physician_edit",
            "supersedes": str(fact_id),
            "reviewer_role": principal.role,
            "reason": (reason or "")[:200] or None,
        },
        verification_status=VerificationStatus.PHYSICIAN_VERIFIED,
        supersedes=fact_id,
        extra_audit={"reason": (reason or "")[:200] or None},
    )
    return await write(conn, principal, fact)


async def mark_verification(
    conn: asyncpg.Connection,
    principal: Principal,
    *,
    fact_id: UUID,
    status: VerificationStatus,
) -> None:
    """Update review status only. Content columns are immutable (§13)."""
    updated = await conn.fetchval(
        """
        UPDATE clinical_fact
           SET verification_status = $2
         WHERE id = $1
        RETURNING id
        """,
        fact_id,
        str(status),
    )
    if updated is None:
        raise NotFound("fact not found", reason_code="not_found")
    await audit.record(
        conn,
        principal,
        action="clinical_fact.verification_changed",
        entity_type="clinical_fact",
        entity_id=fact_id,
        detail={"next_status": str(status)},
    )


async def current_facts(
    conn: asyncpg.Connection, session_id: UUID
) -> list[FactRecord]:
    """Live facts for a session, newest first, superseded rows excluded."""
    rows = await conn.fetch(
        """
        SELECT * FROM clinical_fact
         WHERE session_id = $1 AND superseded_by IS NULL
         ORDER BY created_at, id
        """,
        session_id,
    )
    return [_to_record(r) for r in rows]


async def all_facts_with_history(
    conn: asyncpg.Connection, session_id: UUID
) -> list[FactRecord]:
    """Every fact including superseded ones — the provenance trail (§13)."""
    rows = await conn.fetch(
        "SELECT * FROM clinical_fact WHERE session_id = $1 ORDER BY created_at, id",
        session_id,
    )
    return [_to_record(r) for r in rows]


async def facts_by_ids(
    conn: asyncpg.Connection, fact_ids: list[UUID]
) -> dict[UUID, FactRecord]:
    if not fact_ids:
        return {}
    rows = await conn.fetch(
        "SELECT * FROM clinical_fact WHERE id = ANY($1::uuid[])", fact_ids
    )
    return {r["id"]: _to_record(r) for r in rows}


async def get_fact(conn: asyncpg.Connection, fact_id: UUID) -> FactRecord:
    row = await conn.fetchrow("SELECT * FROM clinical_fact WHERE id = $1", fact_id)
    if row is None:
        raise NotFound("fact not found", reason_code="not_found")
    return _to_record(row)


def _respondent_type(source: SourceType) -> str:
    return {
        SourceType.PATIENT_ANSWER: "patient",
        SourceType.CAREGIVER_ANSWER: "caregiver",
        SourceType.STAFF_ENTRY: "staff",
        SourceType.PHYSICIAN_EDIT: "staff",
        SourceType.DOCUMENT_EXTRACTION: "staff",
    }[source]


def _band(confidence: float) -> str:
    if confidence >= 0.9:
        return "very_high"
    if confidence >= 0.75:
        return "high"
    if confidence >= 0.5:
        return "medium"
    return "low"


def _to_record(row) -> FactRecord:
    return FactRecord(
        id=row["id"],
        session_id=row["session_id"],
        category=row["category"],
        concept_code=row["concept_code"],
        concept_label=row["concept_label"],
        value_raw=row["value_raw"],
        value_normalized=row["value_normalized"],
        unit=row["unit"],
        confidence=float(row["confidence"]),
        source_type=row["source_type"],
        respondent_id=row["respondent_id"],
        respondent_relationship=row["respondent_relationship"],
        provenance_ref=row["provenance_ref"],
        verification_status=row["verification_status"],
        is_conflicting=row["is_conflicting"],
        abnormal_flag=row["abnormal_flag"],
        superseded_by=row["superseded_by"],
        created_at=row["created_at"],
    )
