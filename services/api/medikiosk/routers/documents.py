"""Staff document surface (CLAUDE.md §9, §17.1, §17.2, §24).

Three responsibilities:

* **Staff-assisted capture** — the mandatory no-phone fallback. A named member
  of staff performs it and is recorded as the uploader, so provenance stays
  truthful (§9).
* **Human verification queue** — the confidence gate of §17.2. Extractions below
  the threshold never auto-populate the record; a human accepts, corrects or
  rejects each one, and only then does a clinical fact exist.
* **Document viewing** — short-lived presigned URLs so the physician can read the
  source scan beside the extracted value (§64.5).
"""

from __future__ import annotations

from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, UploadFile
from pydantic import BaseModel, Field as PField

from medikiosk.deps import Ctx, StaffPrincipal, load_session_row, require, session_resource
from medikiosk.errors import Conflict, NotFound, ValidationFailed
from medikiosk.modules.audit import service as audit
from medikiosk.modules.ayush_namaste import service as namaste_service
from medikiosk.modules.clinical_facts import service as facts_service
from medikiosk.modules.clinical_facts.service import (
    FactInput,
    SourceType,
    VerificationStatus,
)
from medikiosk.modules.conflict import service as conflict_service
from medikiosk.modules.document import service as document_service
from medikiosk.modules.timeline import service as timeline_service
from medikiosk.observability.logging_setup import get_logger
from medikiosk.security.opa import ResourceContext
from medikiosk.security.rbac import Capability

log = get_logger(__name__)

router = APIRouter(prefix="/v1/staff", tags=["documents"])


@router.post("/sessions/{session_id}/capture")
async def staff_capture(
    ctx: Ctx,
    session_id: UUID,
    file: Annotated[UploadFile, File()],
    principal: StaffPrincipal,
    authz: Annotated[
        Any, Depends(require(Capability.STAFF_ASSISTED_CAPTURE, "upload_document"))
    ],
    note: Annotated[str | None, Form()] = None,
) -> dict[str, Any]:
    """Staff-assisted capture for a patient with no phone (§9).

    The uploader recorded is the STAFF MEMBER, not the patient — the physician
    must be able to see who photographed the document.
    """
    content = await file.read()
    async with ctx.db.transaction(principal) as conn:
        row = await load_session_row(conn, session_id)
        await authz.check(session_resource(row))

        accepted = await document_service.accept_upload(
            conn,
            principal,
            ctx.scanner,
            session_id=session_id,
            patient_id=row["patient_id"],
            content=content,
            declared_mime=file.content_type,
            original_filename=file.filename,
            capture_path="staff_assisted",
            respondent_type="staff",
            respondent_id=principal.actor_id,
            respondent_relationship=None,
            upload_token_id=None,
            max_bytes=ctx.settings.max_upload_bytes,
        )
        if note:
            await audit.record(
                conn,
                principal,
                action="document.staff_capture_note",
                entity_type="document",
                entity_id=accepted.document_id,
                detail={"reason": note[:200]},
            )
        object_key = await conn.fetchval(
            "SELECT object_key FROM document WHERE id = $1", accepted.document_id
        )

    if accepted.duplicate_of is None and accepted.processing_status != "rejected":
        try:
            await ctx.objects.put(key=object_key, content=content, mime=accepted.verified_mime)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "document_store_deferred",
                component="document",
                document_id=accepted.document_id,
                error_class=type(exc).__name__,
            )
    if accepted.processing_status == "queued":
        await ctx.broker.publish(
            "document.uploaded",
            {"document_id": str(accepted.document_id), "tenant_id": str(principal.tenant_id)},
            idempotency_key=f"doc:{accepted.document_id}",
        )

    return {
        "document_id": str(accepted.document_id),
        "processing_status": accepted.processing_status,
        "quality_status": accepted.quality_status,
        "uploaded_by": "staff",
        "duplicate": accepted.duplicate_of is not None,
    }


@router.get("/sessions/{session_id}/documents")
async def list_documents(
    ctx: Ctx,
    session_id: UUID,
    principal: StaffPrincipal,
    authz: Annotated[Any, Depends(require(Capability.DOCUMENT_READ, "read"))],
) -> dict[str, Any]:
    async with ctx.db.readonly(principal) as conn:
        row = await load_session_row(conn, session_id)
        await authz.check(session_resource(row))
        documents = await document_service.list_for_session(conn, session_id)
    return {"session_id": str(session_id), "documents": documents}


@router.get("/documents/{document_id}")
async def document_detail(
    ctx: Ctx,
    document_id: UUID,
    principal: StaffPrincipal,
    authz: Annotated[Any, Depends(require(Capability.DOCUMENT_READ, "read"))],
) -> dict[str, Any]:
    """Document metadata, OCR pages, and a short-lived viewing URL.

    OCR text is returned to a clinician, clearly marked as machine-read
    UNTRUSTED source text — a physician reading the extraction must be able to
    check it against the image (§19, §64.5).
    """
    async with ctx.db.readonly(principal) as conn:
        document = await document_service.get(conn, document_id)
        session_row = await load_session_row(conn, document["session_id"])
        await authz.check(session_resource(session_row))
        pages = await document_service.pages(conn, document_id)
        candidates = await conn.fetch(
            """
            SELECT id, category, concept_code, concept_label, value_raw, value_normalized,
                   unit, confidence, status, page_number, model_version, resulting_fact_id
              FROM extraction_candidate
             WHERE document_id = $1
             ORDER BY confidence DESC
            """,
            document_id,
        )

    view_url = None
    if document["object_key"]:
        try:
            view_url = await ctx.objects.presigned_get(document["object_key"])
        except Exception:  # noqa: BLE001
            view_url = None

    return {
        "document_id": str(document_id),
        "session_id": str(document["session_id"]),
        "capture_path": document["capture_path"],
        "doc_class": document["doc_class"],
        "processing_status": document["processing_status"],
        "quality_status": document["quality_status"],
        "verified_mime": document["verified_mime"],
        "malware_scan_status": document["malware_scan_status"],
        "uploaded_by": {
            "respondent_type": document["respondent_type"],
            "relationship": document["respondent_relationship"],
        },
        "ocr": {
            "engine": document["ocr_engine"],
            "model_version": document["ocr_model_version"],
            "pages": pages,
            "note": "machine-read source text — verify against the image",
        },
        "extraction_candidates": [dict(c) for c in candidates],
        "view_url": view_url,
        "view_url_expires_seconds": 300 if view_url else None,
    }


@router.get("/verification-queue")
async def verification_queue(
    ctx: Ctx,
    principal: StaffPrincipal,
    authz: Annotated[Any, Depends(require(Capability.EXTRACTION_VERIFY, "read"))],
) -> dict[str, Any]:
    """The confidence-gated human verification queue (§17.2).

    Everything here scored BELOW the auto-accept threshold. Nothing in this queue
    has become a clinical fact yet, and nothing will until a human decides.
    """
    await authz.check(
        ResourceContext(
            type="extraction_candidate",
            tenant_id=principal.tenant_id,
            department_id=principal.department_id,
        )
    )
    async with ctx.db.readonly(principal) as conn:
        rows = await conn.fetch(
            """
            SELECT c.id, c.document_id, c.session_id, c.page_number, c.category,
                   c.concept_code, c.concept_label, c.value_raw, c.value_normalized,
                   c.unit, c.confidence, c.model_version, c.created_at,
                   d.capture_path, d.doc_class, d.quality_status,
                   s.department_id, s.language,
                   p.full_name, p.hospital_local_id
              FROM extraction_candidate c
              JOIN document d ON d.id = c.document_id
              JOIN session s ON s.id = c.session_id
              JOIN patient p ON p.id = s.patient_id
             WHERE c.status = 'pending'
               AND ($1::uuid IS NULL OR s.department_id = $1)
             ORDER BY c.confidence, c.created_at
             LIMIT 200
            """,
            principal.department_id,
        )
    return {
        "threshold": ctx.settings.extraction_auto_accept_threshold,
        "note": (
            "Below-threshold extractions never auto-populate the record; "
            "each requires a human decision (CLAUDE.md §17.2)."
        ),
        "candidates": [
            {
                **dict(r),
                "patient_display": (
                    f"{r['full_name']} ({r['hospital_local_id']})"
                    if r["hospital_local_id"]
                    else r["full_name"]
                ),
            }
            for r in rows
        ],
    }


class VerifyRequest(BaseModel):
    decision: Literal["accept", "correct", "reject"]
    corrected_value: Any = None
    note: str | None = PField(default=None, max_length=500)


@router.post("/verification-queue/{candidate_id}")
async def verify_candidate(
    ctx: Ctx,
    candidate_id: UUID,
    payload: VerifyRequest,
    principal: StaffPrincipal,
    authz: Annotated[Any, Depends(require(Capability.EXTRACTION_VERIFY, "verify"))],
) -> dict[str, Any]:
    """Human decision on a low-confidence extraction (§17.2).

    Accepting or correcting writes a clinical fact with ``document_extraction``
    provenance that records the verifying human. Rejecting writes nothing — the
    candidate is closed and no fact is ever created, which is the correct outcome
    for a misread.
    """
    async with ctx.db.transaction(principal) as conn:
        candidate = await conn.fetchrow(
            "SELECT * FROM extraction_candidate WHERE id = $1", candidate_id
        )
        if candidate is None:
            raise NotFound("candidate not found", reason_code="not_found")
        if candidate["status"] != "pending":
            raise Conflict("candidate already reviewed", reason_code="already_reviewed")

        session_row = await load_session_row(conn, candidate["session_id"])
        await authz.check(session_resource(session_row))

        if payload.decision == "reject":
            await conn.execute(
                """
                UPDATE extraction_candidate
                   SET status = 'human_rejected', reviewed_by = $2, reviewed_at = now()
                 WHERE id = $1
                """,
                candidate_id,
                principal.actor_id,
            )
            await audit.record(
                conn,
                principal,
                action="extraction.rejected",
                entity_type="extraction_candidate",
                entity_id=candidate_id,
                detail={
                    "concept_code": candidate["concept_code"],
                    "reason": (payload.note or "")[:200] or None,
                },
            )
            return {"candidate_id": str(candidate_id), "status": "human_rejected",
                    "fact_created": False}

        if payload.decision == "correct" and payload.corrected_value is None:
            raise ValidationFailed("corrected_value is required",
                                   reason_code="validation_failed")

        value = (
            payload.corrected_value
            if payload.decision == "correct"
            else candidate["value_normalized"]
        )
        status = "human_corrected" if payload.decision == "correct" else "human_accepted"

        fact = await facts_service.write(
            conn,
            principal,
            FactInput(
                session_id=candidate["session_id"],
                patient_id=session_row["patient_id"],
                category=candidate["category"],
                concept_code=candidate["concept_code"],
                concept_label=candidate["concept_label"],
                value_normalized=value,
                value_raw=candidate["value_raw"],
                unit=candidate["unit"],
                # A human-verified value is no longer a probabilistic estimate.
                confidence=1.0 if payload.decision == "correct" else float(candidate["confidence"]),
                source_type=SourceType.DOCUMENT_EXTRACTION,
                respondent_id=None,
                provenance_ref={
                    "method": "document_extraction_human_verified",
                    "document_id": str(candidate["document_id"]),
                    "page": candidate["page_number"],
                    "model_version": candidate["model_version"],
                    "verified_by_role": principal.role,
                    "human_decision": payload.decision,
                },
                verification_status=VerificationStatus.PHYSICIAN_VERIFIED,
                extra_audit={"category": candidate["category"], "outcome": status},
            ),
        )
        await conn.execute(
            """
            UPDATE extraction_candidate
               SET status = $2, reviewed_by = $3, reviewed_at = now(), resulting_fact_id = $4
             WHERE id = $1
            """,
            candidate_id,
            status,
            principal.actor_id,
            fact.id,
        )
        # A newly accepted document fact can contradict what the patient said,
        # and can move the timeline, so both are recomputed here (§15, §16).
        await conflict_service.detect(conn, principal, session_id=candidate["session_id"])
        await timeline_service.rebuild(conn, principal, session_id=candidate["session_id"])

    return {
        "candidate_id": str(candidate_id),
        "status": status,
        "fact_created": True,
        "fact_id": str(fact.id),
    }


# ---------------------------------------------------------------------------
# NAMASTE / ICD-11 TM2 coding (§24)
# ---------------------------------------------------------------------------
class NamasteSuggestRequest(BaseModel):
    fact_id: UUID
    language: str = "en"


@router.post("/namaste/suggest")
async def namaste_suggest(
    ctx: Ctx,
    payload: NamasteSuggestRequest,
    principal: StaffPrincipal,
    authz: Annotated[Any, Depends(require(Capability.NAMASTE_SUGGEST, "suggest_coding"))],
) -> dict[str, Any]:
    """Suggest ranked NAMASTE + ICD-11 TM2 candidates.

    Nothing is written. The response is explicitly labelled as coming from a
    static snapshot, not a live Ministry API (§24 [ASSUMPTION]).
    """
    async with ctx.db.readonly(principal) as conn:
        fact = await facts_service.get_fact(conn, payload.fact_id)
        session_row = await load_session_row(conn, fact.session_id)
        await authz.check(session_resource(session_row))
        result = await namaste_service.suggest(
            conn,
            ctx.ai,
            ctx.terminology,
            fact_id=payload.fact_id,
            version=ctx.settings.terminology_snapshot_version,
            language=ctx.localization.normalize(payload.language),
        )
    return result


class NamasteConfirmRequest(BaseModel):
    namaste_code: str
    ai_suggestion_rank: int | None = None
    ai_suggestion_score: float | None = None


@router.post("/namaste/{fact_id}/confirm")
async def namaste_confirm(
    ctx: Ctx,
    fact_id: UUID,
    payload: NamasteConfirmRequest,
    principal: StaffPrincipal,
    authz: Annotated[Any, Depends(require(Capability.NAMASTE_CONFIRM, "confirm_coding"))],
) -> dict[str, Any]:
    """Record a practitioner-confirmed dual coding (§24).

    Only a mapping confirmed here is ever written or exported.
    """
    async with ctx.db.transaction(principal) as conn:
        fact = await facts_service.get_fact(conn, fact_id)
        session_row = await load_session_row(conn, fact.session_id)
        await authz.check(session_resource(session_row))
        result = await namaste_service.confirm(
            conn,
            principal,
            ctx.terminology,
            fact_id=fact_id,
            namaste_code=payload.namaste_code,
            version=ctx.settings.terminology_snapshot_version,
            ai_suggestion_rank=payload.ai_suggestion_rank,
            ai_suggestion_score=payload.ai_suggestion_score,
        )
    return result


@router.get("/sessions/{session_id}/coding")
async def session_coding(
    ctx: Ctx,
    session_id: UUID,
    principal: StaffPrincipal,
    authz: Annotated[Any, Depends(require(Capability.CLINICAL_READ, "read"))],
) -> dict[str, Any]:
    async with ctx.db.readonly(principal) as conn:
        row = await load_session_row(conn, session_id)
        await authz.check(session_resource(row))
        confirmed = await namaste_service.confirmed_for_session(conn, session_id)
        pending = await namaste_service.uncoded_diagnoses(conn, session_id)
    return {
        "session_id": str(session_id),
        "confirmed": confirmed,
        "awaiting_confirmation": pending,
        "terminology_version": ctx.settings.terminology_snapshot_version,
        "is_live_api": False,
    }
