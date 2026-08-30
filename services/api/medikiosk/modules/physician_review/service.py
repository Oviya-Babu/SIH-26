"""Physician review and approval — the authority gate (CLAUDE.md §21).

    Draft → UnderReview → {Edited, ClarificationRequested, Rejected} → UnderReview
    UnderReview → Approved → Exported

[RED LINE §21] No transition reaches ``Exported`` without passing through
``Approved``. The rule is enforced three times over, deliberately:

1. here, in the service, with an explicit transition table;
2. in the database, by the ``physician_review_transition_guard`` trigger;
3. in OPA, which refuses any write action on a session whose status is exported.

Approval is also what emits the outbox event, in the SAME transaction as the
approval itself (§23, §50) — so an approval can never exist without its export
being queued, and an export can never be queued without an approval.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

import asyncpg

from medikiosk.db import Principal, as_json
from medikiosk.errors import Conflict, NotFound, ValidationFailed
from medikiosk.modules.audit import service as audit
from medikiosk.observability.logging_setup import get_logger

log = get_logger(__name__)


class ReviewStatus(StrEnum):
    DRAFT = "draft"
    UNDER_REVIEW = "under_review"
    EDITED = "edited"
    CLARIFICATION_REQUESTED = "clarification_requested"
    REJECTED = "rejected"
    APPROVED = "approved"
    EXPORTED = "exported"


# The §21 state machine, as data. Mirrors the database trigger exactly; a test
# asserts the two agree, so they cannot drift.
TRANSITIONS: dict[ReviewStatus, tuple[ReviewStatus, ...]] = {
    ReviewStatus.DRAFT: (ReviewStatus.UNDER_REVIEW,),
    ReviewStatus.UNDER_REVIEW: (
        ReviewStatus.EDITED,
        ReviewStatus.CLARIFICATION_REQUESTED,
        ReviewStatus.REJECTED,
        ReviewStatus.APPROVED,
    ),
    ReviewStatus.EDITED: (ReviewStatus.UNDER_REVIEW,),
    ReviewStatus.CLARIFICATION_REQUESTED: (ReviewStatus.UNDER_REVIEW,),
    ReviewStatus.REJECTED: (ReviewStatus.UNDER_REVIEW,),
    ReviewStatus.APPROVED: (ReviewStatus.EXPORTED,),
    ReviewStatus.EXPORTED: (),
}


@dataclass(frozen=True, slots=True)
class Review:
    id: UUID
    session_id: UUID
    summary_id: UUID | None
    status: ReviewStatus
    reviewer_id: UUID | None
    approved_by: UUID | None
    approved_at: Any
    exported_at: Any


async def get(conn: asyncpg.Connection, session_id: UUID) -> Review:
    row = await conn.fetchrow(
        "SELECT * FROM physician_review WHERE session_id = $1", session_id
    )
    if row is None:
        raise NotFound("review not found", reason_code="not_found")
    return _to_review(row)


async def _transition(
    conn: asyncpg.Connection,
    principal: Principal,
    *,
    session_id: UUID,
    to: ReviewStatus,
    columns: str = "",
    params: tuple[Any, ...] = (),
    detail: dict[str, Any] | None = None,
) -> Review:
    current = await get(conn, session_id)
    if to not in TRANSITIONS[current.status]:
        raise Conflict(
            f"illegal review transition {current.status} -> {to}",
            reason_code="illegal_review_transition",
            detail={"previous_status": str(current.status), "next_status": str(to)},
        )

    row = await conn.fetchrow(
        f"""
        UPDATE physician_review
           SET status = $2, updated_at = now(){(", " + columns) if columns else ""}
         WHERE session_id = $1
        RETURNING *
        """,
        session_id,
        str(to),
        *params,
    )
    await audit.record(
        conn,
        principal,
        action=f"review.{to}",
        entity_type="physician_review",
        entity_id=row["id"],
        detail={
            "previous_status": str(current.status),
            "next_status": str(to),
            **(detail or {}),
        },
    )
    return _to_review(row)


async def open_review(
    conn: asyncpg.Connection, principal: Principal, *, session_id: UUID
) -> Review:
    """A physician opens the session. Idempotent: re-opening is not an error."""
    current = await get(conn, session_id)
    if current.status is ReviewStatus.UNDER_REVIEW:
        return current
    if current.status in (ReviewStatus.APPROVED, ReviewStatus.EXPORTED):
        raise Conflict(
            "this session has already been approved",
            reason_code="already_approved",
        )
    return await _transition(
        conn,
        principal,
        session_id=session_id,
        to=ReviewStatus.UNDER_REVIEW,
        columns="reviewer_id = $3, opened_at = COALESCE(opened_at, now())",
        params=(principal.actor_id,),
    )


async def mark_edited(
    conn: asyncpg.Connection, principal: Principal, *, session_id: UUID, fact_id: UUID
) -> Review:
    """Record that a fact was edited, then return to under_review.

    The round trip through ``edited`` exists so the audit trail shows *when*
    editing happened relative to approval, rather than only that the record was
    approved at the end.
    """
    await _transition(
        conn,
        principal,
        session_id=session_id,
        to=ReviewStatus.EDITED,
        detail={"superseded_fact_id": fact_id},
    )
    return await _transition(
        conn, principal, session_id=session_id, to=ReviewStatus.UNDER_REVIEW
    )


async def request_clarification(
    conn: asyncpg.Connection, principal: Principal, *, session_id: UUID, note: str
) -> Review:
    if len(note.strip()) < 4:
        raise ValidationFailed("a clarification note is required",
                               reason_code="validation_failed")
    return await _transition(
        conn,
        principal,
        session_id=session_id,
        to=ReviewStatus.CLARIFICATION_REQUESTED,
        columns="clarification_note = $3",
        params=(note.strip()[:2000],),
        detail={"reason": "clarification_requested"},
    )


async def reject(
    conn: asyncpg.Connection, principal: Principal, *, session_id: UUID, reason: str
) -> Review:
    if len(reason.strip()) < 4:
        raise ValidationFailed("a rejection reason is required",
                               reason_code="validation_failed")
    return await _transition(
        conn,
        principal,
        session_id=session_id,
        to=ReviewStatus.REJECTED,
        columns="rejection_reason = $3",
        params=(reason.strip()[:2000],),
        detail={"reason": reason.strip()[:200]},
    )


async def reopen(
    conn: asyncpg.Connection, principal: Principal, *, session_id: UUID
) -> Review:
    return await _transition(
        conn,
        principal,
        session_id=session_id,
        to=ReviewStatus.UNDER_REVIEW,
        columns="reviewer_id = $3",
        params=(principal.actor_id,),
    )


async def approve(
    conn: asyncpg.Connection,
    principal: Principal,
    *,
    session_id: UUID,
    tenant_id: UUID,
    export_targets: tuple[str, ...] = ("fhir",),
) -> tuple[Review, list[str]]:
    """Approve, and queue the export in the SAME transaction (§23, §50).

    Returns the review and the idempotency keys of the queued outbox events, so
    the caller can report exactly what was scheduled.
    """
    unresolved = await conn.fetchval(
        """
        SELECT count(*) FROM fact_conflict
         WHERE session_id = $1 AND resolution = 'unresolved'
        """,
        session_id,
    )
    if unresolved:
        # §15: conflicts are surfaced, never auto-resolved. Approving over an
        # unadjudicated contradiction would export a record the physician has
        # not actually reconciled.
        raise Conflict(
            f"{unresolved} unresolved conflict(s) must be adjudicated before approval",
            reason_code="unresolved_conflicts",
            detail={"count": int(unresolved)},
        )

    pending_coding = await conn.fetchval(
        """
        SELECT count(*)
          FROM clinical_fact f
         WHERE f.session_id = $1
           AND f.superseded_by IS NULL
           AND f.category = 'diagnosis'
           AND NOT EXISTS (SELECT 1 FROM namaste_mapping m WHERE m.fact_id = f.id)
        """,
        session_id,
    )

    review = await _transition(
        conn,
        principal,
        session_id=session_id,
        to=ReviewStatus.APPROVED,
        columns="approved_by = $3, approved_at = now(), reviewer_id = COALESCE(reviewer_id, $3)",
        params=(principal.actor_id,),
        detail={"count": int(pending_coding or 0)},
    )

    keys: list[str] = []
    for target in export_targets:
        # A deterministic-ish unique key: the session plus target plus a fresh
        # uuid. Downstream adapters treat it as the idempotency token, so a retry
        # of the SAME outbox row can never create a duplicate record (§8 DoD).
        key = f"{target}:{session_id}:{uuid4().hex[:12]}"
        await conn.execute(
            """
            INSERT INTO outbox_event
                (tenant_id, event_type, aggregate_type, aggregate_id, payload, idempotency_key)
            VALUES ($1, $2, 'session', $3, $4::jsonb, $5)
            """,
            tenant_id,
            "ClinicalSummaryApproved",
            session_id,
            as_json(
                {
                    "target": target,
                    "session_id": str(session_id),
                    "approved_by": str(principal.actor_id),
                    "review_id": str(review.id),
                }
            ),
            key,
        )
        keys.append(key)

    log.info(
        "review_approved",
        component="physician_review",
        session_id=session_id,
        tenant_id=tenant_id,
        actor_id=principal.actor_id,
        actor_role=principal.role,
        count=len(keys),
    )
    return review, keys


async def mark_exported(
    conn: asyncpg.Connection,
    principal: Principal,
    *,
    session_id: UUID,
    target: str,
) -> Review:
    """Called by the integration relay after a successful delivery.

    The database trigger will reject this if the record is not ``approved``, so
    the [RED LINE §21] invariant does not depend on this function being correct.
    """
    return await _transition(
        conn,
        principal,
        session_id=session_id,
        to=ReviewStatus.EXPORTED,
        columns="exported_at = now()",
        detail={"target": target},
    )


async def queue(
    conn: asyncpg.Connection,
    *,
    department_id: UUID | None,
    statuses: tuple[str, ...] = (
        "draft",
        "under_review",
        "edited",
        "clarification_requested",
        "rejected",
    ),
) -> list[dict[str, Any]]:
    """The physician's work queue.

    Ordered by clinical urgency, not arrival: an escalated session with a
    critical red flag outranks a complete routine one.
    """
    rows = await conn.fetch(
        """
        SELECT pr.id AS review_id, pr.status, pr.session_id, pr.reviewer_id,
               pr.opened_at, pr.created_at,
               s.status AS session_status, s.completeness, s.language,
               s.fast_path_active, s.protocol_family, s.submitted_at,
               s.respondent_type, s.department_id,
               d.display_name AS department_name,
               p.full_name, p.hospital_local_id, p.year_of_birth, p.gender,
               p.abha_reference IS NOT NULL AS has_abha,
               (SELECT count(*) FROM clinical_fact f
                 WHERE f.session_id = s.id AND f.superseded_by IS NULL) AS fact_count,
               (SELECT count(*) FROM red_flag_alert a
                 WHERE a.session_id = s.id AND a.severity = 'critical') AS critical_alerts,
               (SELECT count(*) FROM red_flag_alert a
                 WHERE a.session_id = s.id AND a.severity = 'high') AS high_alerts,
               (SELECT count(*) FROM fact_conflict c
                 WHERE c.session_id = s.id AND c.resolution = 'unresolved')
                                                                    AS unresolved_conflicts,
               (SELECT count(*) FROM document doc
                 WHERE doc.session_id = s.id
                   AND doc.processing_status NOT IN ('completed', 'rejected'))
                                                                    AS documents_pending,
               (SELECT generation_mode FROM summary sm WHERE sm.session_id = s.id)
                                                                    AS summary_mode
          FROM physician_review pr
          JOIN session s ON s.id = pr.session_id
          JOIN department d ON d.id = s.department_id
          JOIN patient p ON p.id = s.patient_id
         WHERE pr.status = ANY($1::text[])
           AND ($2::uuid IS NULL OR s.department_id = $2)
         ORDER BY (SELECT count(*) FROM red_flag_alert a
                    WHERE a.session_id = s.id AND a.severity = 'critical') DESC,
                  (SELECT count(*) FROM red_flag_alert a
                    WHERE a.session_id = s.id AND a.severity = 'high') DESC,
                  s.submitted_at NULLS LAST,
                  pr.created_at
        """,
        list(statuses),
        department_id,
    )
    return [dict(r) for r in rows]


async def history(conn: asyncpg.Connection, session_id: UUID) -> list[dict[str, Any]]:
    """The review's audit trail — every state change, attributed."""
    rows = await conn.fetch(
        """
        SELECT id, actor_role, action, detail, occurred_at
          FROM audit_event
         WHERE entity_type IN ('physician_review', 'clinical_fact', 'fact_conflict')
           AND (detail ? 'previous_status' OR action LIKE 'clinical_fact.%')
           AND entity_id IN (
                 SELECT id FROM physician_review WHERE session_id = $1
                 UNION SELECT id FROM clinical_fact WHERE session_id = $1
                 UNION SELECT id FROM fact_conflict WHERE session_id = $1
           )
         ORDER BY id
        """,
        session_id,
    )
    return [dict(r) for r in rows]


def _to_review(row) -> Review:
    return Review(
        id=row["id"],
        session_id=row["session_id"],
        summary_id=row["summary_id"],
        status=ReviewStatus(row["status"]),
        reviewer_id=row["reviewer_id"],
        approved_by=row["approved_by"],
        approved_at=row["approved_at"],
        exported_at=row["exported_at"],
    )
