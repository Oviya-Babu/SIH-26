"""Typed API errors.

Error responses carry a stable ``reason_code`` and never carry clinical content
or an internal message that could leak PHI (§28). The kiosk frontend maps
``reason_code`` to a localized, patient-appropriate message; it never renders a
raw backend string to a patient.
"""

from __future__ import annotations

from typing import Any


class MediKioskError(Exception):
    status_code: int = 500
    reason_code: str = "internal_error"

    def __init__(
        self,
        message: str = "",
        *,
        reason_code: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message or self.reason_code)
        if reason_code:
            self.reason_code = reason_code
        self.detail = detail or {}
        self.message = message or self.reason_code


class AuthenticationRequired(MediKioskError):
    status_code = 401
    reason_code = "authentication_required"


class Forbidden(MediKioskError):
    """A real server-side 403 (§5.3, §64.8) — never a hidden UI route."""

    status_code = 403
    reason_code = "forbidden"


class NotFound(MediKioskError):
    status_code = 404
    reason_code = "not_found"


class Conflict(MediKioskError):
    status_code = 409
    reason_code = "conflict"


class ValidationFailed(MediKioskError):
    status_code = 422
    reason_code = "validation_failed"


class ConsentRequired(MediKioskError):
    """Internal MediKiosk consent gates everything MediKiosk does (§7.2)."""

    status_code = 403
    reason_code = "consent_required"


class SessionSealed(MediKioskError):
    """The session is exported; clinical writes are refused (§21)."""

    status_code = 409
    reason_code = "session_sealed_after_export"


class DependencyUnavailable(MediKioskError):
    """An upstream (AI, broker, integration) is down.

    Callers must translate this into the defined degraded-mode behaviour of §37,
    never into a blocked clinical workflow.
    """

    status_code = 503
    reason_code = "dependency_unavailable"


class RateLimited(MediKioskError):
    status_code = 429
    reason_code = "rate_limited"


class PayloadTooLarge(MediKioskError):
    status_code = 413
    reason_code = "payload_too_large"


class UnsupportedMedia(MediKioskError):
    status_code = 415
    reason_code = "unsupported_media_type"
