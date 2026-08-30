"""Document intake and the QR-to-phone handoff (CLAUDE.md §9, §17, §34, §35).

Three capture paths, ONE pipeline (§17.1):

    kiosk camera  ─┐
    QR → phone    ─┼──→ validate → malware scan → object store → RabbitMQ → OCR
    staff-assisted ┘

[RED LINE §9] No anonymous uploads. Whoever uploaded — patient, authorised
caregiver, or staff — is recorded with ``respondent_id`` and relationship,
identically to a spoken answer.

[RED LINE §50] The upload endpoint acknowledges in <500 ms and enqueues. The
patient never waits for OCR, and OCR never runs inside the interactive loop.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

import asyncpg

from medikiosk.db import Principal
from medikiosk.errors import Conflict, Forbidden, NotFound, ValidationFailed
from medikiosk.modules.audit import service as audit
from medikiosk.modules.document.security import (
    MalwareScanner,
    ScanStatus,
    image_quality_verdict,
    validate_bytes,
)
from medikiosk.observability.logging_setup import get_logger
from medikiosk.security.tokens import TokenService

log = get_logger(__name__)

CAPTURE_PATHS = ("kiosk_camera", "qr_phone_upload", "staff_assisted")


@dataclass(frozen=True, slots=True)
class UploadTokenIssue:
    token: str
    token_id: UUID
    expires_at: datetime
    upload_url_path: str
    ttl_seconds: int


async def issue_upload_token(
    conn: asyncpg.Connection,
    principal: Principal,
    tokens: TokenService,
    *,
    session_id: UUID,
    respondent_type: str,
    respondent_id: UUID,
    respondent_relationship: str | None,
    ttl_seconds: int,
) -> UploadTokenIssue:
    """Mint the QR-encoded, upload-only, session-bound token (§9).

    Scope is literally ``document_upload`` and the column has a CHECK constraint
    pinning it there: there is no read scope to escalate to. The token itself is
    never stored — only its digest — so a database read cannot yield a usable
    token.
    """
    session = await conn.fetchrow(
        """
        SELECT s.id, s.status, s.patient_id, pr.status AS review_status
          FROM session s
          LEFT JOIN physician_review pr ON pr.session_id = s.id
         WHERE s.id = $1
        """,
        session_id,
    )
    if session is None:
        raise NotFound("session not found", reason_code="not_found")
    if session["review_status"] == "exported":
        raise Conflict("session is sealed", reason_code="upload_session_closed")
    if session["status"] not in ("in_progress", "escalated_to_staff", "awaiting_confirmation"):
        raise Conflict("session is not accepting uploads",
                       reason_code="upload_session_closed")

    token, claims = tokens.mint(
        "upload",
        tenant_id=principal.tenant_id,
        ttl_seconds=ttl_seconds,
        session_id=session_id,
        patient_id=session["patient_id"],
        subject_role=respondent_type,
        respondent_relationship=respondent_relationship,
    )

    token_id = await conn.fetchval(
        """
        INSERT INTO upload_token
            (tenant_id, session_id, token_hash, respondent_type, respondent_id,
             respondent_relationship, expires_at)
        VALUES ($1, $2, $3, $4, $5, $6, to_timestamp($7))
        RETURNING id
        """,
        principal.tenant_id,
        session_id,
        TokenService.hash_token(token),
        respondent_type,
        respondent_id,
        respondent_relationship,
        claims.expires_at,
    )
    await audit.record(
        conn,
        principal,
        action="upload_token.issued",
        entity_type="upload_token",
        entity_id=token_id,
        detail={"respondent_type": respondent_type, "capture_path": "qr_phone_upload"},
    )
    return UploadTokenIssue(
        token=token,
        token_id=token_id,
        expires_at=datetime.fromtimestamp(claims.expires_at, tz=timezone.utc),
        upload_url_path="/upload",
        ttl_seconds=claims.expires_at - claims.issued_at,
    )


@dataclass(frozen=True, slots=True)
class UploadTokenContext:
    token_id: UUID
    session_id: UUID
    patient_id: UUID
    tenant_id: UUID
    respondent_type: str
    respondent_id: UUID
    respondent_relationship: str | None


async def resolve_upload_token(
    conn: asyncpg.Connection,
    *,
    token: str,
    tenant_id: UUID,
    session_id: UUID,
) -> UploadTokenContext:
    """Validate a phone-presented upload token (§9, §34).

    Replay protection: the token is bound to one session, its digest must still
    exist unrevoked, it must not be expired, and the session must still be open.
    A token reused after the session closes is rejected server-side — the phone
    page cannot tell the difference, and does not need to.
    """
    row = await conn.fetchrow(
        """
        SELECT t.id, t.session_id, t.respondent_type, t.respondent_id,
               t.respondent_relationship, t.expires_at, t.revoked_at, t.use_count,
               s.patient_id, s.status AS session_status, s.tenant_id,
               pr.status AS review_status
          FROM upload_token t
          JOIN session s ON s.id = t.session_id
          LEFT JOIN physician_review pr ON pr.session_id = s.id
         WHERE t.token_hash = $1 AND t.session_id = $2
        """,
        TokenService.hash_token(token),
        session_id,
    )
    if row is None:
        raise Forbidden("upload token is not recognised", reason_code="invalid_signature")
    if row["revoked_at"] is not None:
        raise Forbidden("upload token has been revoked", reason_code="token_expired")
    if row["expires_at"] <= datetime.now(timezone.utc):
        raise Forbidden("upload token has expired", reason_code="token_expired")
    if row["review_status"] == "exported":
        raise Conflict("session is sealed", reason_code="upload_session_closed")
    if row["session_status"] not in (
        "in_progress",
        "escalated_to_staff",
        "awaiting_confirmation",
    ):
        raise Conflict("session is closed", reason_code="upload_session_closed")

    return UploadTokenContext(
        token_id=row["id"],
        session_id=row["session_id"],
        patient_id=row["patient_id"],
        tenant_id=row["tenant_id"],
        respondent_type=row["respondent_type"],
        respondent_id=row["respondent_id"],
        respondent_relationship=row["respondent_relationship"],
    )


async def revoke_session_tokens(
    conn: asyncpg.Connection, principal: Principal, *, session_id: UUID
) -> int:
    """Revoke all upload tokens for a session (called at submission)."""
    rows = await conn.fetch(
        """
        UPDATE upload_token SET revoked_at = now()
         WHERE session_id = $1 AND revoked_at IS NULL
        RETURNING id
        """,
        session_id,
    )
    for row in rows:
        await audit.record(
            conn,
            principal,
            action="upload_token.revoked",
            entity_type="upload_token",
            entity_id=row["id"],
            detail={"reason_code": "session_closed"},
        )
    return len(rows)


@dataclass(frozen=True, slots=True)
class AcceptedDocument:
    document_id: UUID
    verified_mime: str
    size_bytes: int
    sha256: str
    quality_status: str
    malware_scan_status: str
    processing_status: str
    duplicate_of: UUID | None


async def accept_upload(
    conn: asyncpg.Connection,
    principal: Principal,
    scanner: MalwareScanner | None,
    *,
    session_id: UUID,
    patient_id: UUID,
    content: bytes,
    declared_mime: str | None,
    original_filename: str | None,
    capture_path: str,
    respondent_type: str,
    respondent_id: UUID,
    respondent_relationship: str | None,
    upload_token_id: UUID | None,
    max_bytes: int,
) -> AcceptedDocument:
    """Validate, scan and record an uploaded document.

    Returns as soon as the row is written (§9: ack < 500 ms). The actual OCR is
    enqueued by the caller after commit, so the patient's phone gets its "received"
    immediately.
    """
    if capture_path not in CAPTURE_PATHS:
        raise ValidationFailed("unknown capture path", reason_code="validation_failed")

    validated = validate_bytes(content, declared_mime=declared_mime, max_bytes=max_bytes)

    # Re-photographing the same paper is common; recording it once keeps the
    # physician's document list honest instead of showing four copies.
    duplicate = await conn.fetchval(
        """
        SELECT id FROM document
         WHERE session_id = $1 AND sha256 = $2
         LIMIT 1
        """,
        session_id,
        validated.sha256,
    )
    if duplicate is not None:
        return AcceptedDocument(
            document_id=duplicate,
            verified_mime=validated.verified_mime,
            size_bytes=validated.size_bytes,
            sha256=validated.sha256,
            quality_status="ok",
            malware_scan_status="clean",
            processing_status="queued",
            duplicate_of=duplicate,
        )

    quality = image_quality_verdict(content, validated.verified_mime)

    # [RED LINE §35] scan BEFORE the file can enter the OCR pipeline.
    scan_status = ScanStatus.PENDING
    scanner_name = None
    if scanner is not None:
        scan_status, detail = await scanner.scan(content)
        scanner_name = "clamav"
        if scan_status is ScanStatus.INFECTED:
            document_id = await _insert_document(
                conn,
                principal,
                session_id=session_id,
                patient_id=patient_id,
                capture_path=capture_path,
                respondent_type=respondent_type,
                respondent_id=respondent_id,
                respondent_relationship=respondent_relationship,
                upload_token_id=upload_token_id,
                original_filename=original_filename,
                declared_mime=declared_mime,
                validated=validated,
                object_key="",  # infected content is never stored
                malware_scan_status="infected",
                scanner=scanner_name,
                quality=quality,
                processing_status="rejected",
                processing_error=detail[:200],
            )
            raise UnsupportedMediaInfected(document_id)

    if scanner is None or scan_status is ScanStatus.ERROR:
        # Held, not passed through. An unscanned file must never reach a parser.
        processing_status = "scanning"
    elif quality == "unreadable":
        processing_status = "needs_recapture"
    else:
        processing_status = "queued"

    object_key = f"{principal.tenant_id}/{session_id}/{uuid4().hex}"
    document_id = await _insert_document(
        conn,
        principal,
        session_id=session_id,
        patient_id=patient_id,
        capture_path=capture_path,
        respondent_type=respondent_type,
        respondent_id=respondent_id,
        respondent_relationship=respondent_relationship,
        upload_token_id=upload_token_id,
        original_filename=original_filename,
        declared_mime=declared_mime,
        validated=validated,
        object_key=object_key,
        malware_scan_status=str(scan_status),
        scanner=scanner_name,
        quality=quality,
        processing_status=processing_status,
        processing_error=None,
    )

    if upload_token_id is not None:
        await conn.execute(
            "UPDATE upload_token SET use_count = use_count + 1 WHERE id = $1",
            upload_token_id,
        )

    return AcceptedDocument(
        document_id=document_id,
        verified_mime=validated.verified_mime,
        size_bytes=validated.size_bytes,
        sha256=validated.sha256,
        quality_status=quality,
        malware_scan_status=str(scan_status),
        processing_status=processing_status,
        duplicate_of=None,
    )


class UnsupportedMediaInfected(Conflict):
    """Raised when malware is detected. The document row exists, rejected."""

    reason_code = "malware_detected"

    def __init__(self, document_id: UUID) -> None:
        super().__init__(
            "file was rejected by the malware scanner",
            reason_code="malware_detected",
            detail={"document_id": str(document_id)},
        )


async def _insert_document(
    conn: asyncpg.Connection,
    principal: Principal,
    *,
    session_id: UUID,
    patient_id: UUID,
    capture_path: str,
    respondent_type: str,
    respondent_id: UUID,
    respondent_relationship: str | None,
    upload_token_id: UUID | None,
    original_filename: str | None,
    declared_mime: str | None,
    validated,
    object_key: str,
    malware_scan_status: str,
    scanner: str | None,
    quality: str,
    processing_status: str,
    processing_error: str | None,
) -> UUID:
    document_id = await conn.fetchval(
        """
        INSERT INTO document
            (tenant_id, session_id, patient_id, capture_path, respondent_type,
             respondent_id, respondent_relationship, upload_token_id, original_filename,
             declared_mime, verified_mime, size_bytes, sha256, object_key,
             malware_scan_status, malware_scanner, quality_status, processing_status,
             processing_error)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16,
                $17, $18, $19)
        RETURNING id
        """,
        principal.tenant_id,
        session_id,
        patient_id,
        capture_path,
        respondent_type,
        respondent_id,
        respondent_relationship,
        upload_token_id,
        (original_filename or "")[:255] or None,
        declared_mime,
        validated.verified_mime,
        validated.size_bytes,
        validated.sha256,
        object_key,
        malware_scan_status,
        scanner,
        quality,
        processing_status,
        processing_error,
    )
    await audit.record(
        conn,
        principal,
        action="document.uploaded",
        entity_type="document",
        entity_id=document_id,
        detail={
            "capture_path": capture_path,
            "respondent_type": respondent_type,
            "verified_mime": validated.verified_mime,
            "size_bytes": validated.size_bytes,
            "malware_scan_status": malware_scan_status,
            "quality_status": quality,
            "status": processing_status,
        },
    )
    log.info(
        "document_accepted",
        component="document",
        session_id=session_id,
        tenant_id=principal.tenant_id,
        document_id=document_id,
        capture_path=capture_path,
        verified_mime=validated.verified_mime,
        size_bytes=validated.size_bytes,
        malware_scan_status=malware_scan_status,
        quality_status=quality,
        status=processing_status,
    )
    return document_id


async def store_object(store, *, object_key: str, content: bytes, mime: str) -> None:
    """Persist the original to object storage.

    Encryption at rest is KMS/Vault-managed (§32); the bucket policy, not this
    call, is what enforces it.
    """
    if store is None:
        return
    await store.put(object_key, content, mime)


async def list_for_session(
    conn: asyncpg.Connection, session_id: UUID
) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        """
        SELECT id, capture_path, doc_class, processing_status, quality_status,
               verified_mime, size_bytes, pages, respondent_type,
               respondent_relationship, malware_scan_status, ocr_engine,
               ocr_model_version, processing_error, created_at, processed_at
          FROM document
         WHERE session_id = $1
         ORDER BY created_at
        """,
        session_id,
    )
    return [dict(r) for r in rows]


async def get(conn: asyncpg.Connection, document_id: UUID) -> dict[str, Any]:
    row = await conn.fetchrow("SELECT * FROM document WHERE id = $1", document_id)
    if row is None:
        raise NotFound("document not found", reason_code="not_found")
    return dict(row)


async def pages(conn: asyncpg.Connection, document_id: UUID) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        """
        SELECT page_number, ocr_text, ocr_confidence, handwritten, layout
          FROM document_page
         WHERE document_id = $1
         ORDER BY page_number
        """,
        document_id,
    )
    return [dict(r) for r in rows]


async def mark_status(
    conn: asyncpg.Connection,
    principal: Principal,
    *,
    document_id: UUID,
    status: str,
    error: str | None = None,
) -> None:
    await conn.execute(
        """
        UPDATE document
           SET processing_status = $2,
               processing_error = $3,
               processed_at = CASE WHEN $2 IN ('completed', 'failed', 'rejected')
                                  THEN now() ELSE processed_at END
         WHERE id = $1
        """,
        document_id,
        status,
        error,
    )
    await audit.record(
        conn,
        principal,
        action="document.status_changed",
        entity_type="document",
        entity_id=document_id,
        detail={"next_status": status, "reason": (error or "")[:200] or None},
    )


def encode_content(content: bytes) -> str:
    return base64.b64encode(content).decode("ascii")
