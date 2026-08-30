"""File upload security (CLAUDE.md §35).

Four controls, applied in this order, before a byte ever reaches the OCR
pipeline:

1. **Size cap** — cheapest check first.
2. **Magic-byte verification** — the declared ``Content-Type`` is never trusted.
   A ``.jpg`` that is really a PDF, an SVG, or an HTML page with a script is
   rejected on its actual bytes.
3. **Content-type allowlist** — only ``image/jpeg``, ``image/png`` and
   ``application/pdf``. An allowlist, not a denylist.
4. **Malware scan** — [RED LINE §35] performed BEFORE the file enters the OCR
   pipeline. If the scanner is unreachable the document is HELD, not passed
   through: an unscanned file must never reach a downstream parser.
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from enum import StrEnum

from medikiosk.errors import PayloadTooLarge, UnsupportedMedia
from medikiosk.observability.logging_setup import get_logger

log = get_logger(__name__)

ALLOWED_MIME: frozenset[str] = frozenset({"image/jpeg", "image/png", "application/pdf"})

# Signatures checked against the actual leading bytes.
_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"%PDF-", "application/pdf"),
)

# Shapes that must be refused even if a caller labels them as an allowed type.
# An SVG is an image to a browser and a script host to an attacker.
_DANGEROUS_PREFIXES: tuple[tuple[bytes, str], ...] = (
    (b"<?xml", "xml_or_svg"),
    (b"<svg", "svg"),
    (b"<!DOCTYPE html", "html"),
    (b"<html", "html"),
    (b"PK\x03\x04", "zip_or_office"),
    (b"\x7fELF", "elf_binary"),
    (b"MZ", "windows_executable"),
    (b"#!", "script"),
    (b"\xd0\xcf\x11\xe0", "ole_compound"),
)


class ScanStatus(StrEnum):
    CLEAN = "clean"
    INFECTED = "infected"
    ERROR = "error"
    PENDING = "pending"


@dataclass(frozen=True, slots=True)
class ValidatedUpload:
    verified_mime: str
    size_bytes: int
    sha256: str


def validate_bytes(
    content: bytes,
    *,
    declared_mime: str | None,
    max_bytes: int,
) -> ValidatedUpload:
    """Verify a file on its CONTENT, not its label."""
    size = len(content)
    if size == 0:
        raise UnsupportedMedia("file is empty", reason_code="unsupported_media_type")
    if size > max_bytes:
        raise PayloadTooLarge(
            f"file exceeds {max_bytes} bytes", reason_code="payload_too_large"
        )

    head = content[:64]
    for prefix, label in _DANGEROUS_PREFIXES:
        if head.startswith(prefix):
            log.warning(
                "upload_rejected_dangerous_content",
                component="document",
                reason_code=f"dangerous_{label}",
                size_bytes=size,
            )
            raise UnsupportedMedia(
                "file type is not accepted", reason_code="unsupported_media_type"
            )

    verified: str | None = None
    for signature, mime in _SIGNATURES:
        if content.startswith(signature):
            verified = mime
            break

    if verified is None:
        log.info(
            "upload_rejected_unknown_signature",
            component="document",
            reason_code="magic_byte_mismatch",
            size_bytes=size,
        )
        raise UnsupportedMedia(
            "file content does not match an accepted type",
            reason_code="unsupported_media_type",
        )
    if verified not in ALLOWED_MIME:
        raise UnsupportedMedia("file type is not accepted",
                               reason_code="unsupported_media_type")

    # A mismatch between declared and verified is logged but not fatal: phone
    # cameras mislabel constantly, and the VERIFIED type is what we use anyway.
    if declared_mime and declared_mime.split(";")[0].strip().lower() != verified:
        log.info(
            "upload_declared_mime_mismatch",
            component="document",
            verified_mime=verified,
            reason_code="declared_mime_mismatch",
        )

    return ValidatedUpload(
        verified_mime=verified,
        size_bytes=size,
        sha256=hashlib.sha256(content).hexdigest(),
    )


class MalwareScanner:
    """ClamAV INSTREAM client.

    ``required=True`` means an unreachable scanner yields ``ERROR`` and the
    document is HELD in ``scanning`` — never promoted to the OCR pipeline. That
    is the fail-closed behaviour §35 demands.
    """

    def __init__(self, host: str, port: int, *, required: bool = True,
                 timeout: float = 30.0) -> None:
        self._host = host
        self._port = port
        self._required = required
        self._timeout = timeout

    @property
    def required(self) -> bool:
        return self._required

    async def scan(self, content: bytes) -> tuple[ScanStatus, str]:
        try:
            return await asyncio.wait_for(self._scan(content), timeout=self._timeout)
        except (asyncio.TimeoutError, OSError, ConnectionError) as exc:
            log.error(
                "malware_scan_unavailable",
                component="document",
                error_class=type(exc).__name__,
                malware_scan_status="error",
            )
            return ScanStatus.ERROR, f"scanner_unavailable:{type(exc).__name__}"

    async def _scan(self, content: bytes) -> tuple[ScanStatus, str]:
        reader, writer = await asyncio.open_connection(self._host, self._port)
        try:
            writer.write(b"zINSTREAM\x00")
            # ClamAV INSTREAM: length-prefixed chunks, terminated by a zero length.
            chunk_size = 64 * 1024
            for offset in range(0, len(content), chunk_size):
                chunk = content[offset : offset + chunk_size]
                writer.write(len(chunk).to_bytes(4, "big") + chunk)
            writer.write((0).to_bytes(4, "big"))
            await writer.drain()
            response = (await reader.read(4096)).decode("utf-8", "replace").strip("\x00 \n")
        finally:
            writer.close()
            with_suppress = getattr(writer, "wait_closed", None)
            if with_suppress is not None:
                try:
                    await writer.wait_closed()
                except Exception:  # noqa: BLE001
                    pass

        if response.endswith("OK") and "FOUND" not in response:
            return ScanStatus.CLEAN, "clamav:ok"
        if "FOUND" in response:
            log.warning(
                "malware_detected", component="document", malware_scan_status="infected"
            )
            return ScanStatus.INFECTED, response[:200]
        return ScanStatus.ERROR, response[:200]


def image_quality_verdict(content: bytes, verified_mime: str) -> str:
    """Cheap pre-OCR quality triage (§17.2).

    Not a substitute for the gateway's real quality check — it catches the
    obvious cases (a nearly-empty capture) locally so the patient is asked to
    retake immediately rather than after a two-minute async round trip.
    """
    if verified_mime == "application/pdf":
        return "ok"
    # A JPEG under ~12 KB from a phone camera is almost always a lens-covered or
    # black frame; asking for a retake now saves the patient a wasted wait.
    if len(content) < 12_000:
        return "unreadable"
    return "ok"
