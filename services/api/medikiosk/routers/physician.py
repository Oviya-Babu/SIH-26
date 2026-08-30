"""Physician / AYUSH practitioner review API (CLAUDE.md §21, §24, §49).

The physician is the authority in this system. Every endpoint here is a physician
*act*, recorded as such, and nothing downstream happens without one.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field as PField

from medikiosk.deps import Ctx, StaffPrincipal, load_session_row, require, session_resource
from medikiosk.errors import Forbidden, NotFound
from medikiosk.modules.ayush_namaste import service as namaste_service
from medikiosk.modules.clinical_facts import service as facts_service
from medikiosk.modules.conflict import service as conflict_service
from medikiosk.modules.physician_review import service as review_service
from medikiosk.modules.session import service as session_service
from medikiosk.modules.summary import service as summary_service
from medikiosk.modules.timeline import service as timeline_service
from medikiosk.modules.triage import service as triage_service
from medikiosk.observability.logging_setup import get_logger
from medikiosk.security.opa import ResourceContext
from medikiosk.security.rbac import Capability

log = get_logger(__name__)

router = APIRouter(prefix="/v1", tags=["physician"])


@router.get("/reviews")
async def review_queue(
    ctx: Ctx,
    principal: StaffPrincipal,
    authz: Annotated[Any, Depends(require(Capability.REVIEW_QUEUE_READ, "read"))],
) -> dict[str, Any]:
    """The physician's queue, ordered by clinical urgency (§4)."""
    await authz.check(
        ResourceContext(
            type="physician_review",
            tenant_id=principal.tenant_id,
            department_id=principal.department_id,
        )
    )
    async with ctx.db.readonly(principal) as conn:
        rows = await review_service.queue(conn, department_id=principal.department_id)

    return {
        "department_id": str(principal.department_id) if principal.department_id else None,
        "counts": {
            "total": len(rows),
            "with_critical_flag": sum(1 for r in rows if r["critical_alerts"]),
            "with_unresolved_conflicts": sum(1 for r in rows if r["unresolved_conflicts"]),
            "documents_pending": sum(1 for r in rows if r["documents_pending"]),
        },
        "reviews": [
            {
                "review_id": str(r["review_id"]),
                "session_id": str(r["session_id"]),
                "status": r["status"],
                "session_status": r["session_status"],
                "completeness": float(r["completeness"]),
                "language": r["language"],
                "protocol_family": r["protocol_family"],
                "fast_path_active": r["fast_path_active"],
                "respondent_type": r["respondent_type"],
                "department_name": r["department_name"],
                "patient": {
                    "display": (
                        f"{r['full_name']} ({r['hospital_local_id']})"
                        if r["hospital_local_id"]
                        else r["full_name"]
                    ),
                    "year_of_birth": r["year_of_birth"],
                    "gender": r["gender"],
                    "has_abha": r["has_abha"],
                },
                "signals": {
                    "fact_count": int(r["fact_count"]),
                    "critical_alerts": int(r["critical_alerts"]),
                    "high_alerts": int(r["high_alerts"]),
                    "unresolved_conflicts": int(r["unresolved_conflicts"]),
                    "documents_pending": int(r["documents_pending"]),
                    "summary_mode": r["summary_mode"],
                },
                "submitted_at": r["submitted_at"],
            }
            for r in rows
        ],
    }


@router.post("/reviews/{session_id}/open")
async def open_review(
    ctx: Ctx,
    session_id: UUID,
    principal: StaffPrincipal,
    authz: Annotated[Any, Depends(require(Capability.REVIEW_OPEN, "open_review"))],
) -> dict[str, Any]:
    async with ctx.db.transaction(principal) as conn:
        row = await load_session_row(conn, session_id)
        await authz.check(session_resource(row))
        review = await review_service.open_review(conn, principal, session_id=session_id)
    return {"session_id": str(session_id), "status": str(review.status)}


@router.get("/reviews/{session_id}")
async def review_detail(
    ctx: Ctx,
    session_id: UUID,
    principal: StaffPrincipal,
    authz: Annotated[Any, Depends(require(Capability.CLINICAL_READ, "read"))],
) -> dict[str, Any]:
    """Everything the physician needs on one screen.

    Structured facts, the evidence-cited summary (or the structured fallback),
    the timeline with its separate undated bucket, unresolved conflicts, red-flag
    history, documents, and the provenance of every fact — including which facts a
    caregiver reported rather than the patient (§6, §13, §16, §19).
    """
    async with ctx.db.readonly(principal) as conn:
        row = await load_session_row(conn, session_id)
        await authz.check(session_resource(row))

        session = await session_service.get_snapshot(conn, session_id)
        review = await review_service.get(conn, session_id)
        protocol = ctx.protocols.load(session.protocol_family, session.protocol_version)

        history = await facts_service.all_facts_with_history(conn, session_id)
        conflicts = await conflict_service.list_for_session(conn, session_id)
        timeline = await timeline_service.get(conn, session_id)
        alerts = await triage_service.session_alerts(conn, session_id)
        answers = await session_service.answered_summary(conn, session_id)
        documents = await conn.fetch(
            """
            SELECT id, capture_path, doc_class, processing_status, quality_status,
                   verified_mime, pages, respondent_type, respondent_relationship,
                   ocr_engine, created_at, processed_at
              FROM document WHERE session_id = $1 ORDER BY created_at
            """,
            session_id,
        )
        patient = await conn.fetchrow(
            """
            SELECT full_name, year_of_birth, gender, hospital_local_id,
                   abha_reference IS NOT NULL AS has_abha, preferred_language
              FROM patient WHERE id = $1
            """,
            session.patient_id,
        )
        caregiver = None
        if session.caregiver_auth_id:
            caregiver = await conn.fetchrow(
                """
                SELECT caregiver_name, relationship, authority_basis,
                       patient_acknowledged_at IS NOT NULL AS acknowledged
                  FROM caregiver_authorization WHERE id = $1
                """,
                session.caregiver_auth_id,
            )

        try:
            summary = await summary_service.get(conn, session_id)
        except NotFound:
            summary = None

        namaste = await namaste_service.confirmed_for_session(conn, session_id)

    # Escalation-skipped questions are reported as their own list so the
    # physician can never mistake them for "the patient did not know" (§14.4).
    not_asked = [
        a["field_id"]
        for a in answers
        if a["skip_reason"] == "not_asked_due_to_emergency_escalation"
    ]
    patient_unsure = [
        a["field_id"] for a in answers if a["skip_reason"] in ("patient_unsure", "patient_declined")
    ]

    def _label(field_id: str) -> str:
        field = protocol.fields.get(field_id)
        if field is None:
            return field_id
        return ctx.localization.render_field(protocol, field, "en").touch_label

    return {
        "session": {
            "session_id": str(session_id),
            "status": session.status,
            "review_status": str(review.status),
            "completeness": session.completeness,
            "protocol": {
                "family": session.protocol_family,
                "version": session.protocol_version,
                "checksum": protocol.content_checksum[:12],
            },
            "language": session.language,
            "fast_path_active": session.fast_path_active,
            "respondent_type": session.respondent_type,
        },
        "patient": dict(patient) if patient else None,
        "caregiver": (
            {
                **dict(caregiver),
                "label": (
                    f"Reported by: {caregiver['caregiver_name']}, "
                    f"relationship: {caregiver['relationship']}"
                ),
            }
            if caregiver
            else None
        ),
        "summary": summary,
        "facts": [
            {
                "fact_id": str(f.id),
                "field_id": f.concept_label,
                "label": _label(f.concept_label),
                "category": f.category,
                "concept_code": f.concept_code,
                "value": f.value_normalized,
                "unit": f.unit,
                "confidence": f.confidence,
                "source_type": f.source_type,
                "respondent_relationship": f.respondent_relationship,
                "verification_status": f.verification_status,
                "is_conflicting": f.is_conflicting,
                "abnormal_flag": f.abnormal_flag,
                "superseded_by": str(f.superseded_by) if f.superseded_by else None,
                "provenance": f.provenance_ref,
                "created_at": f.created_at,
            }
            for f in history
        ],
        "conflicts": conflicts,
        "timeline": timeline,
        "red_flags": alerts,
        "documents": [dict(d) for d in documents],
        "coding": namaste,
        "gaps": {
            "not_asked_due_to_escalation": [
                {"field_id": fid, "label": _label(fid)} for fid in not_asked
            ],
            "patient_did_not_know": [
                {"field_id": fid, "label": _label(fid)} for fid in patient_unsure
            ],
        },
        "authority_note": (
            "AI drafting is assistive only. No fact is final until you approve it, "
            "and nothing is exported before approval (CLAUDE.md §19, §21)."
        ),
    }


class EditFactRequest(BaseModel):
    value: Any
    value_raw: str | None = None
    reason: str | None = PField(default=None, max_length=500)


@router.patch("/summaries/{session_id}/facts/{fact_id}")
async def edit_fact(
    ctx: Ctx,
    session_id: UUID,
    fact_id: UUID,
    payload: EditFactRequest,
    principal: StaffPrincipal,
    authz: Annotated[Any, Depends(require(Capability.REVIEW_EDIT_FACT, "edit_fact"))],
) -> dict[str, Any]:
    """Correct a fact.

    [RED LINE §13] This creates a NEW superseding fact. The patient's original
    answer is preserved and stays visible in the provenance trail — a physician
    edit is an additional clinical opinion, not an erasure of what was said.
    """
    async with ctx.db.transaction(principal) as conn:
        row = await load_session_row(conn, session_id)
        await authz.check(session_resource(row))

        original = await facts_service.get_fact(conn, fact_id)
        if original.session_id != session_id:
            raise Forbidden("fact does not belong to this session", reason_code="forbidden")

        await review_service.open_review(conn, principal, session_id=session_id)
        new_fact = await facts_service.supersede_with_physician_edit(
            conn,
            principal,
            fact_id=fact_id,
            value_normalized=payload.value,
            value_raw=payload.value_raw,
            reason=payload.reason,
        )
        await review_service.mark_edited(
            conn, principal, session_id=session_id, fact_id=fact_id
        )
        # The correction can change the timeline and can resolve or create a
        # conflict, so both are recomputed inside the same transaction.
        await timeline_service.rebuild(conn, principal, session_id=session_id)
        await conflict_service.detect(conn, principal, session_id=session_id)

    return {
        "superseded_fact_id": str(fact_id),
        "new_fact_id": str(new_fact.id),
        "source_type": new_fact.source_type,
        "note": "the original answer is preserved and remains visible",
    }


class StatementActionRequest(BaseModel):
    section: str
    ordinal: int
    action: Literal["accepted", "edited", "excluded"]
    edited_text: str | None = PField(default=None, max_length=2000)


@router.patch("/summaries/{session_id}/statements")
async def statement_action(
    ctx: Ctx,
    session_id: UUID,
    payload: StatementActionRequest,
    principal: StaffPrincipal,
    authz: Annotated[Any, Depends(require(Capability.REVIEW_EDIT_FACT, "edit_fact"))],
) -> dict[str, Any]:
    async with ctx.db.transaction(principal) as conn:
        row = await load_session_row(conn, session_id)
        await authz.check(session_resource(row))
        review = await review_service.get(conn, session_id)
        if review.summary_id is None:
            raise NotFound("summary not generated yet", reason_code="summary_not_ready")
        await summary_service.set_statement_action(
            conn,
            principal,
            summary_id=review.summary_id,
            section=payload.section,
            ordinal=payload.ordinal,
            action=payload.action,
            edited_text=payload.edited_text,
        )
    return {"section": payload.section, "ordinal": payload.ordinal, "action": payload.action}


class ConflictResolutionRequest(BaseModel):
    resolution: Literal[
        "physician_chose_a", "physician_chose_b", "physician_entered_new", "not_a_conflict"
    ]


@router.post("/reviews/{session_id}/conflicts/{conflict_id}/resolve")
async def resolve_conflict(
    ctx: Ctx,
    session_id: UUID,
    conflict_id: UUID,
    payload: ConflictResolutionRequest,
    principal: StaffPrincipal,
    authz: Annotated[Any, Depends(require(Capability.CONFLICT_RESOLVE, "resolve_conflict"))],
) -> dict[str, Any]:
    """Adjudicate a contradiction.

    [RED LINE §15] Only a physician resolves a conflict. There is no automatic
    resolution anywhere in the system.
    """
    async with ctx.db.transaction(principal) as conn:
        row = await load_session_row(conn, session_id)
        await authz.check(session_resource(row))
        result = await conflict_service.resolve(
            conn, principal, conflict_id=conflict_id, resolution=payload.resolution
        )
    return {"conflict_id": str(conflict_id), "resolution": result["resolution"]}


class ClarificationRequest(BaseModel):
    note: str = PField(min_length=4, max_length=2000)


@router.post("/reviews/{session_id}/request-clarification")
async def request_clarification(
    ctx: Ctx,
    session_id: UUID,
    payload: ClarificationRequest,
    principal: StaffPrincipal,
    authz: Annotated[
        Any, Depends(require(Capability.REVIEW_REQUEST_CLARIFICATION, "request_clarification"))
    ],
) -> dict[str, Any]:
    async with ctx.db.transaction(principal) as conn:
        row = await load_session_row(conn, session_id)
        await authz.check(session_resource(row))
        review = await review_service.request_clarification(
            conn, principal, session_id=session_id, note=payload.note
        )
    return {"session_id": str(session_id), "status": str(review.status)}


class RejectRequest(BaseModel):
    reason: str = PField(min_length=4, max_length=2000)


@router.post("/reviews/{session_id}/reject")
async def reject_review(
    ctx: Ctx,
    session_id: UUID,
    payload: RejectRequest,
    principal: StaffPrincipal,
    authz: Annotated[Any, Depends(require(Capability.REVIEW_REJECT, "reject"))],
) -> dict[str, Any]:
    async with ctx.db.transaction(principal) as conn:
        row = await load_session_row(conn, session_id)
        await authz.check(session_resource(row))
        review = await review_service.reject(
            conn, principal, session_id=session_id, reason=payload.reason
        )
    return {"session_id": str(session_id), "status": str(review.status)}


@router.post("/reviews/{session_id}/reopen")
async def reopen_review(
    ctx: Ctx,
    session_id: UUID,
    principal: StaffPrincipal,
    authz: Annotated[Any, Depends(require(Capability.REVIEW_OPEN, "open_review"))],
) -> dict[str, Any]:
    async with ctx.db.transaction(principal) as conn:
        row = await load_session_row(conn, session_id)
        await authz.check(session_resource(row))
        review = await review_service.reopen(conn, principal, session_id=session_id)
    return {"session_id": str(session_id), "status": str(review.status)}


class ApproveRequest(BaseModel):
    export_targets: list[Literal["fhir", "abdm", "his"]] = PField(default_factory=lambda: ["fhir"])
    attestation: bool = False


@router.post("/summaries/{session_id}/approve")
async def approve(
    ctx: Ctx,
    session_id: UUID,
    payload: ApproveRequest,
    principal: StaffPrincipal,
    authz: Annotated[Any, Depends(require(Capability.REVIEW_APPROVE, "approve"))],
) -> dict[str, Any]:
    """The authority gate (§21).

    Approval and the queuing of its export happen in the SAME transaction, so an
    approval can never exist without a queued export and an export can never be
    queued without an approval.

    ``attestation`` must be explicitly true: approving a clinical record is a
    deliberate act, and a click-through default would undermine that.
    """
    if not payload.attestation:
        raise Forbidden(
            "approval requires explicit clinician attestation",
            reason_code="attestation_required",
        )

    async with ctx.db.transaction(principal) as conn:
        row = await load_session_row(conn, session_id)
        await authz.check(session_resource(row))
        review, keys = await review_service.approve(
            conn,
            principal,
            session_id=session_id,
            tenant_id=principal.tenant_id,
            export_targets=tuple(payload.export_targets),
        )

    return {
        "session_id": str(session_id),
        "status": str(review.status),
        "approved_by": str(review.approved_by),
        "queued_exports": keys,
        "environment": ctx.settings.abdm_environment,
        "is_sandbox": ctx.settings.abdm_environment == "sandbox",
    }


@router.get("/reviews/{session_id}/history")
async def review_history(
    ctx: Ctx,
    session_id: UUID,
    principal: StaffPrincipal,
    authz: Annotated[Any, Depends(require(Capability.CLINICAL_READ, "read"))],
) -> dict[str, Any]:
    async with ctx.db.readonly(principal) as conn:
        row = await load_session_row(conn, session_id)
        await authz.check(session_resource(row))
        events = await review_service.history(conn, session_id)
    return {"session_id": str(session_id), "events": events}


@router.post("/summaries/{session_id}/regenerate")
async def regenerate_summary(
    ctx: Ctx,
    session_id: UUID,
    principal: StaffPrincipal,
    authz: Annotated[Any, Depends(require(Capability.REVIEW_EDIT_FACT, "edit_fact"))],
) -> dict[str, Any]:
    """Regenerate the draft after new documents or edits.

    Never blocks: if the LLM is unavailable the structured fallback is written
    instead, and the response says which mode was used (§19, §37).
    """
    ai = getattr(ctx, "ai", None)
    async with ctx.db.transaction(principal) as conn:
        row = await load_session_row(conn, session_id)
        await authz.check(session_resource(row))
        await timeline_service.rebuild(conn, principal, session_id=session_id)
        await conflict_service.detect(conn, principal, session_id=session_id)
        generated = await summary_service.generate(
            conn,
            principal,
            ai,
            ctx.localization,
            session_id=session_id,
            language="en",
        )
    return {
        "summary_id": str(generated.summary_id),
        "generation_mode": generated.generation_mode,
        "statement_count": generated.statement_count,
        "citation_count": generated.citation_count,
        "dropped_uncited": generated.dropped_uncited,
        "latency_ms": generated.latency_ms,
    }
