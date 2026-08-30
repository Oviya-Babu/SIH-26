"""QR-to-phone document upload (CLAUDE.md §9, §34, §35).

Two surfaces here:

* ``/v1/sessions/{id}/upload-token`` — the KIOSK asks for a token and renders the
  QR code. Requires a session token.
* ``/v1/upload/*`` — the PHONE's surface. Authenticated by the upload token
  alone, which is upload-only, single-session and short-TTL. It deliberately has
  no read endpoint for clinical data: there is nothing here to escalate to.

§9: the ack must land in under 500 ms, so the handler validates, scans, records,
and returns — OCR is enqueued after the response is already on its way back to
the patient's phone.
"""

from __future__ import annotations

import base64
import io
from typing import Annotated, Any
from uuid import UUID

import qrcode
from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from pydantic import BaseModel

from medikiosk.db import Principal
from medikiosk.deps import Ctx, SessionPrincipal, load_session_row, require, session_resource
from medikiosk.errors import AuthenticationRequired, Forbidden
from medikiosk.modules.document import service as document_service
from medikiosk.observability.logging_setup import get_logger
from medikiosk.security.rbac import Capability
from medikiosk.security.tokens import TokenError

log = get_logger(__name__)

router = APIRouter(prefix="/v1", tags=["documents"])


class UploadTokenResponse(BaseModel):
    session_id: UUID
    qr_png_base64: str
    upload_path: str
    expires_in_seconds: int
    expires_at: str
    # The kiosk shows this so the patient knows how long they have (§9).
    ttl_minutes: int
    fallback_available: bool


@router.post("/sessions/{session_id}/upload-token", response_model=UploadTokenResponse)
async def issue_upload_token(
    ctx: Ctx,
    session_id: UUID,
    request: Request,
    principal: SessionPrincipal,
    authz: Annotated[
        Any, Depends(require(Capability.UPLOAD_TOKEN_ISSUE, "issue_upload_token", tier="session"))
    ],
) -> UploadTokenResponse:
    """Mint the QR-encoded upload token and render the QR image server-side.

    Rendering the QR here rather than in the browser means the token never sits
    in frontend JavaScript state where a screenshot tool or an extension could
    read it.
    """
    if principal.session_id != session_id:
        raise Forbidden("token is not scoped to this session", reason_code="forbidden")

    async with ctx.db.transaction(principal) as conn:
        row = await load_session_row(conn, session_id)
        await authz.check(session_resource(row))

        issued = await document_service.issue_upload_token(
            conn,
            principal,
            ctx.tokens,
            session_id=session_id,
            respondent_type=(
                "caregiver" if principal.role == "caregiver_respondent" else "patient"
            ),
            respondent_id=principal.patient_id or row["patient_id"],
            respondent_relationship=None,
            ttl_seconds=ctx.settings.upload_token_ttl_seconds,
        )

    base_url = str(request.base_url).rstrip("/")
    payload = f"{base_url}/upload?s={session_id}&t={issued.token}"

    image = qrcode.make(payload)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")

    return UploadTokenResponse(
        session_id=session_id,
        qr_png_base64=base64.b64encode(buffer.getvalue()).decode("ascii"),
        upload_path=issued.upload_url_path,
        expires_in_seconds=issued.ttl_seconds,
        expires_at=issued.expires_at.isoformat(),
        ttl_minutes=max(1, issued.ttl_seconds // 60),
        # §9: staff-assisted capture is a MANDATORY designed fallback, not an
        # improvisation. The kiosk always shows the "I have no phone" route.
        fallback_available=True,
    )


async def _upload_context(
    ctx: Ctx,
    session_id: UUID,
    token: str,
) -> tuple[Principal, document_service.UploadTokenContext]:
    """Authenticate a phone upload from its token alone."""
    try:
        claims = ctx.tokens.verify(token, expect="upload")
    except TokenError as exc:
        raise AuthenticationRequired(exc.message, reason_code=exc.reason_code) from exc

    if claims.session_id != session_id:
        raise Forbidden("token is not scoped to this session", reason_code="wrong_token_kind")

    principal = Principal(
        tenant_id=claims.tenant_id,
        role="patient" if claims.subject_role != "caregiver" else "caregiver_respondent",
        actor_id=claims.patient_id,
        patient_id=claims.patient_id,
        session_id=claims.session_id,
        authorized_session_ids=(claims.session_id,) if claims.session_id else (),
    )
    async with ctx.db.readonly(principal) as conn:
        resolved = await document_service.resolve_upload_token(
            conn, token=token, tenant_id=claims.tenant_id, session_id=session_id
        )
    return principal, resolved


@router.get("/upload/context")
async def upload_context(
    ctx: Ctx,
    s: UUID,
    t: str,
) -> dict[str, Any]:
    """What the phone page needs to render itself, and nothing more.

    Deliberately returns no patient name, no clinical data and no session
    content — an upload token grants the right to SEND, not to read.
    """
    _, resolved = await _upload_context(ctx, s, t)
    language = "en"
    async with ctx.db.readonly(
        Principal(tenant_id=resolved.tenant_id, role="patient",
                  patient_id=resolved.patient_id, session_id=resolved.session_id)
    ) as conn:
        language = await conn.fetchval(
            "SELECT language FROM session WHERE id = $1", resolved.session_id
        ) or "en"

    return {
        "session_id": str(resolved.session_id),
        "language": language,
        "accepted_types": sorted(ctx.settings.allowed_upload_mime),
        "max_bytes": ctx.settings.max_upload_bytes,
        "scope": "document_upload_only",
        "i18n_path": f"/v1/kiosk/i18n/{language}?bundles=kiosk&bundles=errors",
    }


class PhoneUploadResponse(BaseModel):
    document_id: UUID
    processing_status: str
    quality_status: str
    verified_mime: str
    size_bytes: int
    duplicate: bool
    message_key: str
    # The patient may leave immediately; OCR is fully decoupled (§54).
    patient_may_continue: bool


@router.post("/upload/documents", response_model=PhoneUploadResponse)
async def phone_upload(
    ctx: Ctx,
    s: Annotated[UUID, Form()],
    t: Annotated[str, Form()],
    file: Annotated[UploadFile, File()],
) -> PhoneUploadResponse:
    """Receive a photograph from the patient's phone (§9).

    Validated on magic bytes, malware-scanned, then acknowledged. The response
    does not wait for OCR — that is the whole point of the async boundary.
    """
    principal, resolved = await _upload_context(ctx, s, t)
    content = await file.read()

    async with ctx.db.transaction(principal) as conn:
        accepted = await document_service.accept_upload(
            conn,
            principal,
            ctx.scanner,
            session_id=resolved.session_id,
            patient_id=resolved.patient_id,
            content=content,
            declared_mime=file.content_type,
            original_filename=file.filename,
            capture_path="qr_phone_upload",
            respondent_type=resolved.respondent_type,
            respondent_id=resolved.respondent_id,
            respondent_relationship=resolved.respondent_relationship,
            upload_token_id=resolved.token_id,
            max_bytes=ctx.settings.max_upload_bytes,
        )

    return await _finalise_upload(ctx, principal, accepted, content)


@router.post("/sessions/{session_id}/documents", response_model=PhoneUploadResponse)
async def kiosk_upload(
    ctx: Ctx,
    session_id: UUID,
    file: Annotated[UploadFile, File()],
    principal: SessionPrincipal,
    authz: Annotated[
        Any, Depends(require(Capability.DOCUMENT_UPLOAD, "upload_document", tier="session"))
    ],
) -> PhoneUploadResponse:
    """Kiosk-camera capture — the default path (§17.1)."""
    if principal.session_id != session_id:
        raise Forbidden("token is not scoped to this session", reason_code="forbidden")

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
            capture_path="kiosk_camera",
            respondent_type=(
                "caregiver" if principal.role == "caregiver_respondent" else "patient"
            ),
            respondent_id=principal.patient_id or row["patient_id"],
            respondent_relationship=None,
            upload_token_id=None,
            max_bytes=ctx.settings.max_upload_bytes,
        )

    return await _finalise_upload(ctx, principal, accepted, content)


async def _finalise_upload(
    ctx: Ctx,
    principal: Principal,
    accepted: document_service.AcceptedDocument,
    content: bytes,
) -> PhoneUploadResponse:
    """Store the original and enqueue OCR, AFTER the row is committed.

    Order matters: the durable record is the ``document`` row with its
    ``processing_status``. If the object store or the broker is down, the row
    still exists and a sweeper picks it up — the upload is delayed, never lost
    (§37).
    """
    if accepted.duplicate_of is None and accepted.processing_status != "rejected":
        try:
            await ctx.objects.put(
                key=(
                    await _object_key(ctx, principal, accepted.document_id)
                ),
                content=content,
                mime=accepted.verified_mime,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "document_store_deferred",
                component="document",
                document_id=accepted.document_id,
                error_class=type(exc).__name__,
                fallback_engaged=True,
            )

    if accepted.processing_status == "queued":
        published = await ctx.broker.publish(
            "document.uploaded",
            {
                "document_id": str(accepted.document_id),
                "tenant_id": str(principal.tenant_id),
            },
            idempotency_key=f"doc:{accepted.document_id}",
        )
        if not published:
            log.info(
                "document_enqueue_deferred",
                component="document",
                document_id=accepted.document_id,
                fallback_engaged=True,
            )

    message_key = {
        "queued": "documents.upload_processing",
        "scanning": "documents.upload_processing",
        "needs_recapture": "documents.upload_needs_recapture",
        "rejected": "documents.upload_failed",
    }.get(accepted.processing_status, "documents.upload_received")

    return PhoneUploadResponse(
        document_id=accepted.document_id,
        processing_status=accepted.processing_status,
        quality_status=accepted.quality_status,
        verified_mime=accepted.verified_mime,
        size_bytes=accepted.size_bytes,
        duplicate=accepted.duplicate_of is not None,
        message_key=message_key,
        patient_may_continue=True,
    )


async def _object_key(ctx: Ctx, principal: Principal, document_id: UUID) -> str:
    async with ctx.db.readonly(principal) as conn:
        return await conn.fetchval("SELECT object_key FROM document WHERE id = $1", document_id)


@router.get("/upload/status")
async def upload_status(ctx: Ctx, s: UUID, t: str) -> dict[str, Any]:
    """Let the phone page show progress without granting any clinical read.

    Returns processing state only — never OCR text, never extracted values.
    """
    principal, resolved = await _upload_context(ctx, s, t)
    async with ctx.db.readonly(principal) as conn:
        documents = await document_service.list_for_session(conn, resolved.session_id)
    return {
        "session_id": str(resolved.session_id),
        "documents": [
            {
                "document_id": str(d["id"]),
                "processing_status": d["processing_status"],
                "quality_status": d["quality_status"],
                "capture_path": d["capture_path"],
            }
            for d in documents
        ],
    }


# The staff-assisted capture fallback (§9, §17.1) lives on the STAFF surface, in
# routers/documents.py, because the point of that path is that a named member of
# staff performs the capture and is recorded as the uploader.
