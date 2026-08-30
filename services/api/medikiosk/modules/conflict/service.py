"""Conflict detection and lab abnormality (CLAUDE.md §15).

    Conflict(a,b) = concept(a)=concept(b) ∧ normalized(a)≠normalized(b)
                    ∧ both currently asserted

    AbnormalFlag(lab, ref_range) = high/low/normal — a pure comparison against a
                                   governed reference table, NEVER AI-inferred

[RED LINE §15] Conflicts are **surfaced, never auto-resolved**. This module can
create a conflict and can record a physician's adjudication of one. It has no
code path that picks a winner, and adding one would be a red-line violation, not
a feature.

The most valuable conflicts in practice are patient-vs-document: the patient says
they take one medicine, the prescription photograph says another. Both are true
statements about different moments, and only a clinician can reconcile them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import asyncpg

from medikiosk.db import Principal
from medikiosk.errors import Conflict as ConflictError
from medikiosk.errors import NotFound, ValidationFailed
from medikiosk.modules.audit import service as audit
from medikiosk.observability.logging_setup import get_logger

log = get_logger(__name__)

# Source pairs worth flagging. A patient re-answering the same question is
# handled by superseding (§13) and is not a conflict; a DIFFERENT source
# disagreeing is.
_INTERESTING_PAIRS: frozenset[frozenset[str]] = frozenset(
    {
        frozenset({"patient_answer", "document_extraction"}),
        frozenset({"caregiver_answer", "document_extraction"}),
        frozenset({"patient_answer", "caregiver_answer"}),
        frozenset({"staff_entry", "document_extraction"}),
        frozenset({"document_extraction", "document_extraction"}),
    }
)


@dataclass(frozen=True, slots=True)
class DetectedConflict:
    id: UUID
    concept_code: str
    fact_a_id: UUID
    fact_b_id: UUID


def _comparable(value: Any) -> Any:
    """Normalise a stored value for equality comparison.

    Multi-select order must not create a phantom conflict, and a numeric 5 and
    "5" are the same clinical assertion.
    """
    if isinstance(value, dict):
        if "codes" in value and isinstance(value["codes"], list):
            return tuple(sorted(str(v).lower() for v in value["codes"]))
        for key in ("code", "value", "text", "date"):
            if key in value:
                inner = value[key]
                return str(inner).strip().lower() if isinstance(inner, str) else inner
        return tuple(sorted((k, str(v)) for k, v in value.items()))
    if isinstance(value, str):
        return value.strip().lower()
    return value


async def detect(
    conn: asyncpg.Connection,
    principal: Principal,
    *,
    session_id: UUID,
) -> list[DetectedConflict]:
    """Find and record contradictions among a session's live facts.

    Idempotent: re-running after new document extractions adds only the new
    pairs. Existing adjudications are never reset, so a physician's decision is
    not undone by a later re-scan.
    """
    rows = await conn.fetch(
        """
        SELECT id, concept_code, value_normalized, source_type, unit
          FROM clinical_fact
         WHERE session_id = $1 AND superseded_by IS NULL
         ORDER BY created_at, id
        """,
        session_id,
    )

    by_concept: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_concept.setdefault(row["concept_code"], []).append(dict(row))

    detected: list[DetectedConflict] = []
    for concept_code, facts in by_concept.items():
        if len(facts) < 2:
            continue
        for i in range(len(facts)):
            for j in range(i + 1, len(facts)):
                a, b = facts[i], facts[j]
                if frozenset({a["source_type"], b["source_type"]}) not in _INTERESTING_PAIRS:
                    continue
                if _comparable(a["value_normalized"]) == _comparable(b["value_normalized"]):
                    continue

                conflict_id = await conn.fetchval(
                    """
                    INSERT INTO fact_conflict
                        (tenant_id, session_id, concept_code, fact_a_id, fact_b_id)
                    VALUES ($1, $2, $3, $4, $5)
                    ON CONFLICT (fact_a_id, fact_b_id) DO NOTHING
                    RETURNING id
                    """,
                    principal.tenant_id,
                    session_id,
                    concept_code,
                    a["id"],
                    b["id"],
                )
                if conflict_id is None:
                    continue

                group = uuid4()
                await conn.execute(
                    """
                    UPDATE clinical_fact
                       SET is_conflicting = true, conflict_group_id = COALESCE(conflict_group_id, $2)
                     WHERE id = ANY($1::uuid[])
                    """,
                    [a["id"], b["id"]],
                    group,
                )
                await audit.record(
                    conn,
                    principal,
                    action="conflict.detected",
                    entity_type="fact_conflict",
                    entity_id=conflict_id,
                    detail={"concept_code": concept_code, "conflict_id": conflict_id},
                )
                detected.append(
                    DetectedConflict(
                        id=conflict_id,
                        concept_code=concept_code,
                        fact_a_id=a["id"],
                        fact_b_id=b["id"],
                    )
                )

    if detected:
        log.info(
            "conflicts_detected",
            component="conflict",
            session_id=session_id,
            tenant_id=principal.tenant_id,
            count=len(detected),
        )
    return detected


async def list_for_session(
    conn: asyncpg.Connection, session_id: UUID
) -> list[dict[str, Any]]:
    """Conflicts with both sides expanded, for side-by-side adjudication."""
    rows = await conn.fetch(
        """
        SELECT c.id, c.concept_code, c.resolution, c.resolved_at, c.resolved_by,
               a.id AS a_id, a.value_normalized AS a_value, a.source_type AS a_source,
               a.confidence AS a_confidence, a.provenance_ref AS a_provenance,
               a.respondent_relationship AS a_relationship, a.created_at AS a_created,
               b.id AS b_id, b.value_normalized AS b_value, b.source_type AS b_source,
               b.confidence AS b_confidence, b.provenance_ref AS b_provenance,
               b.respondent_relationship AS b_relationship, b.created_at AS b_created
          FROM fact_conflict c
          JOIN clinical_fact a ON a.id = c.fact_a_id
          JOIN clinical_fact b ON b.id = c.fact_b_id
         WHERE c.session_id = $1
         ORDER BY c.resolution = 'unresolved' DESC, c.created_at
        """,
        session_id,
    )
    return [
        {
            "conflict_id": r["id"],
            "concept_code": r["concept_code"],
            "resolution": r["resolution"],
            "resolved_at": r["resolved_at"],
            "side_a": {
                "fact_id": r["a_id"],
                "value": r["a_value"],
                "source_type": r["a_source"],
                "confidence": float(r["a_confidence"]),
                "provenance": r["a_provenance"],
                "respondent_relationship": r["a_relationship"],
                "created_at": r["a_created"],
            },
            "side_b": {
                "fact_id": r["b_id"],
                "value": r["b_value"],
                "source_type": r["b_source"],
                "confidence": float(r["b_confidence"]),
                "provenance": r["b_provenance"],
                "respondent_relationship": r["b_relationship"],
                "created_at": r["b_created"],
            },
        }
        for r in rows
    ]


VALID_RESOLUTIONS = (
    "physician_chose_a",
    "physician_chose_b",
    "physician_entered_new",
    "not_a_conflict",
)


async def resolve(
    conn: asyncpg.Connection,
    principal: Principal,
    *,
    conflict_id: UUID,
    resolution: str,
) -> dict[str, Any]:
    """Record a PHYSICIAN's adjudication.

    The engine never calls this. There is no automatic resolution path anywhere
    in the module — the physician chooses a side, enters a new value, or declares
    it was not a conflict at all.
    """
    if resolution not in VALID_RESOLUTIONS:
        raise ValidationFailed("unknown resolution", reason_code="validation_failed")

    current = await conn.fetchrow(
        "SELECT resolution, fact_a_id, fact_b_id, concept_code FROM fact_conflict WHERE id = $1",
        conflict_id,
    )
    if current is None:
        raise NotFound("conflict not found", reason_code="not_found")
    if current["resolution"] != "unresolved":
        raise ConflictError("conflict is already adjudicated", reason_code="already_resolved")

    row = await conn.fetchrow(
        """
        UPDATE fact_conflict
           SET resolution = $2, resolved_by = $3, resolved_at = now()
         WHERE id = $1
        RETURNING id, resolution, fact_a_id, fact_b_id
        """,
        conflict_id,
        resolution,
        principal.actor_id,
    )

    # Clear the flag on the side the physician did not choose, but never delete
    # the fact: the losing assertion remains in the record with its provenance.
    if resolution in ("physician_chose_a", "physician_chose_b", "not_a_conflict"):
        await conn.execute(
            "UPDATE clinical_fact SET is_conflicting = false WHERE id = ANY($1::uuid[])",
            [row["fact_a_id"], row["fact_b_id"]],
        )

    await audit.record(
        conn,
        principal,
        action="conflict.resolved",
        entity_type="fact_conflict",
        entity_id=conflict_id,
        detail={
            "resolution": resolution,
            "concept_code": current["concept_code"],
            "conflict_id": conflict_id,
        },
    )
    return dict(row)


# ---------------------------------------------------------------------------
# Lab abnormality — deterministic comparison only (§15)
# ---------------------------------------------------------------------------
async def classify_lab_value(
    conn: asyncpg.Connection,
    *,
    analyte_code: str,
    value: float,
    unit: str,
    sex: str | None,
    age_years: int | None,
) -> str | None:
    """Classify against the governed reference table.

    AI's ONLY role in a lab value is extracting the raw number and unit from a
    document. The classification is this comparison — no model, no inference, no
    "probably high". If no reference range exists for the analyte/unit/age/sex
    combination, the answer is ``None``: unknown, not "normal".
    """
    row = await conn.fetchrow(
        """
        SELECT low, high, critical_low, critical_high
          FROM lab_reference_range
         WHERE analyte_code = $1
           AND unit = $2
           AND (sex = 'any' OR sex = COALESCE($3, 'any'))
           AND $4 BETWEEN age_min_years AND age_max_years
         ORDER BY CASE WHEN sex = 'any' THEN 1 ELSE 0 END
         LIMIT 1
        """,
        analyte_code,
        unit,
        sex,
        age_years if age_years is not None else 30,
    )
    if row is None:
        return None

    if row["critical_low"] is not None and value <= float(row["critical_low"]):
        return "critical"
    if row["critical_high"] is not None and value >= float(row["critical_high"]):
        return "critical"
    if row["low"] is not None and value < float(row["low"]):
        return "low"
    if row["high"] is not None and value > float(row["high"]):
        return "high"
    return "normal"


async def abnormal_values(
    conn: asyncpg.Connection, session_id: UUID
) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        """
        SELECT id, concept_code, concept_label, value_normalized, unit, abnormal_flag,
               provenance_ref
          FROM clinical_fact
         WHERE session_id = $1
           AND superseded_by IS NULL
           AND category = 'investigation_value'
           AND abnormal_flag IS NOT NULL
           AND abnormal_flag <> 'normal'
         ORDER BY CASE abnormal_flag WHEN 'critical' THEN 0 ELSE 1 END, concept_code
        """,
        session_id,
    )
    return [dict(r) for r in rows]
