"""PHI/PII redaction — the single choke point (CLAUDE.md §28).

The rule this module implements: **the clinical database may contain necessary
PHI under controlled access; operational telemetry never does** [RED LINE].

Two mechanisms, deliberately layered:

1. **Allowlist** — the default is *don't log*. A structured-log key is emitted
   only if it appears in ``ALLOWED_KEYS``. This is what makes the control hold
   for fields nobody thought to write a pattern for.
2. **Pattern sweep** — any string that survives is still swept for
   identifier-shaped content, because a free-text ``message`` or an exception
   string can carry PHI that no key-level rule would catch.

Identifiers that operations genuinely needs (which session? which patient?) are
kept *useful but pseudonymous*: a tenant-salted HMAC, stable within a tenant so
traces correlate, and not reversible to a patient without the salt (§28).
"""

from __future__ import annotations

import hashlib
import hmac
import re
from typing import Any

REDACTED = "[REDACTED]"

# -----------------------------------------------------------------------------
# Allowlist. Adding a key here is a privacy decision, not a convenience.
# -----------------------------------------------------------------------------
ALLOWED_KEYS: frozenset[str] = frozenset(
    {
        # log plumbing
        "event",
        "level",
        "logger",
        "timestamp",
        "service",
        "environment",
        "exc_info",
        "exception_type",
        # tracing
        "trace_id",
        "span_id",
        "request_id",
        "idempotency_key",
        # request shape (never the body)
        "http_method",
        "http_route",
        "http_status",
        "duration_ms",
        "client_kind",
        # tenancy and actors — pseudonymous forms only
        "tenant_ref",
        "actor_ref",
        "actor_role",
        "patient_ref",
        "session_ref",
        "device_ref",
        "document_ref",
        "fact_ref",
        "respondent_type",
        "respondent_relationship_present",
        # domain events, non-identifying
        "action",
        "entity_type",
        "outcome",
        "reason_code",
        "protocol_family",
        "protocol_version",
        "department_code",
        "language",
        "field_id",
        "category",
        "concept_code",
        "rule_id",
        "ruleset_version",
        "severity",
        "fired",
        "status",
        "previous_status",
        "next_status",
        "completeness",
        "confidence_band",
        "generation_mode",
        "model_version",
        "citation_count",
        "statement_count",
        "queue_depth",
        "attempts",
        "target",
        "capture_path",
        "verified_mime",
        "size_bytes",
        "pages",
        "malware_scan_status",
        "quality_status",
        "abnormal_flag",
        "opa_decision",
        "policy_path",
        "auth_stage",
        "mfa_satisfied",
        "count",
        "purged",
        "component",
        "latency_class",
        "fallback_engaged",
        "error_class",
        "sla_seconds",
        "elapsed_seconds",
    }
)

# Keys whose *values* must be replaced with a pseudonymous reference rather than
# dropped, because operations needs correlation. Mapped to their emitted name.
PSEUDONYMISE_KEYS: dict[str, str] = {
    "tenant_id": "tenant_ref",
    "actor_id": "actor_ref",
    "user_id": "actor_ref",
    "patient_id": "patient_ref",
    "session_id": "session_ref",
    "device_id": "device_ref",
    "document_id": "document_ref",
    "fact_id": "fact_ref",
}

# Keys that must never be emitted in any form, even pseudonymised. Present so
# that an accidental `log.info("x", full_name=...)` fails loudly in tests.
FORBIDDEN_KEYS: frozenset[str] = frozenset(
    {
        "full_name",
        "name",
        "patient_name",
        "abha_reference",
        "abha_address",
        "aadhaar",
        "aadhaar_number",
        "phone",
        "phone_number",
        "mobile",
        "email",
        "address",
        "value_raw",
        "value_normalized",
        "transcript",
        "ocr_text",
        "answer",
        "answers",
        "chief_complaint",
        "staff_message",
        "summary_text",
        "text",
        "password",
        "token",
        "access_token",
        "authorization",
        "secret",
        "api_key",
        "credential",
        "dob",
        "date_of_birth",
        "year_of_birth",
    }
)

# -----------------------------------------------------------------------------
# Pattern sweep. Ordered most-specific first.
# -----------------------------------------------------------------------------
_PATTERNS: tuple[tuple[str, re.Pattern[str]], None] | tuple[tuple[str, re.Pattern[str]], ...] = (
    # Aadhaar-shaped: 12 digits starting 2-9, optionally space/dash grouped.
    # MediKiosk never stores one (§7.1); this exists to catch a leak attempt.
    ("aadhaar", re.compile(r"\b[2-9]\d{3}[ -]?\d{4}[ -]?\d{4}\b")),
    # ABHA address (user@abdm / user@sbx) and 14-digit ABHA number.
    ("abha_address", re.compile(r"\b[\w.\-]{3,}@(?:abdm|sbx|ndhm)\b", re.IGNORECASE)),
    ("abha_number", re.compile(r"\b\d{2}-?\d{4}-?\d{4}-?\d{4}\b")),
    # Indian mobile numbers, with or without +91.
    ("phone", re.compile(r"(?:\+?91[ -]?)?\b[6-9]\d{9}\b")),
    ("email", re.compile(r"\b[\w.+\-]+@[\w\-]+\.[\w.\-]+\b")),
    # Bearer tokens / JWTs.
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{5,}")),
    ("bearer", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{8,}")),
    # Postgres/AMQP/Redis URLs with embedded credentials.
    ("dsn", re.compile(r"(?i)\b(?:postgres(?:ql)?|amqp|redis|mongodb)://[^\s\"']*:[^\s\"'@]*@")),
    # Bare UUIDs in free text: could be a patient id.
    ("uuid", re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
                        r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")),
    # Dates of birth in free text.
    ("date", re.compile(r"\b(?:19|20)\d{2}[-/](?:0?[1-9]|1[0-2])[-/](?:0?[1-9]|[12]\d|3[01])\b")),
)

_MAX_STRING_LEN = 512


def sweep_string(value: str) -> str:
    """Replace identifier-shaped substrings with a typed redaction marker."""
    if len(value) > _MAX_STRING_LEN:
        value = value[:_MAX_STRING_LEN] + "…[TRUNCATED]"
    for label, pattern in _PATTERNS:
        value = pattern.sub(f"[REDACTED:{label}]", value)
    return value


class Pseudonymiser:
    """Tenant-salted, non-reversible reference generator (§28).

    The salt is per-deployment and, for tenant-scoped values, mixed with the
    tenant id, so the same patient id in two tenants yields different refs and a
    telemetry backend cannot join across tenants.
    """

    __slots__ = ("_salt",)

    def __init__(self, salt: str) -> None:
        self._salt = salt.encode("utf-8")

    def ref(self, value: object, *, scope: str = "") -> str:
        raw = f"{scope}|{value}".encode()
        digest = hmac.new(self._salt, raw, hashlib.sha256).hexdigest()
        return digest[:16]


def band_confidence(confidence: float | None) -> str | None:
    """Confidence as a band, not a value: a value can be a fingerprint."""
    if confidence is None:
        return None
    if confidence >= 0.9:
        return "very_high"
    if confidence >= 0.75:
        return "high"
    if confidence >= 0.5:
        return "medium"
    return "low"


class RedactionError(RuntimeError):
    """Raised in synthetic-data environments when a forbidden key is logged.

    In production the key is silently dropped (never logged); in local/ci/test we
    raise, so the violation is caught by a test rather than by an auditor.
    """


def redact_event(
    event: dict[str, Any],
    *,
    pseudonymiser: Pseudonymiser,
    strict: bool,
) -> dict[str, Any]:
    """Apply allowlist + pseudonymisation + pattern sweep to one log event."""
    tenant_scope = str(event.get("tenant_id", ""))
    out: dict[str, Any] = {}
    violations: list[str] = []

    for key, value in event.items():
        if key in FORBIDDEN_KEYS:
            violations.append(key)
            continue

        if key in PSEUDONYMISE_KEYS:
            if value is None:
                continue
            emitted = PSEUDONYMISE_KEYS[key]
            scope = "tenant" if key == "tenant_id" else tenant_scope
            out[emitted] = pseudonymiser.ref(value, scope=scope)
            continue

        if key == "confidence":
            band = band_confidence(_as_float(value))
            if band:
                out["confidence_band"] = band
            continue

        if key not in ALLOWED_KEYS:
            # Default deny. Not an error: unknown keys are simply not telemetry.
            continue

        out[key] = _redact_value(value)

    if violations and strict:
        raise RedactionError(
            "attempted to log PHI-carrying keys: " + ", ".join(sorted(violations))
        )
    if violations:
        out["reason_code"] = "redacted_forbidden_keys"

    return out


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return sweep_string(value)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, (list, tuple)):
        return [_redact_value(v) for v in value[:20]]
    if isinstance(value, dict):
        # Nested structures are not allowlisted; collapse to a shape summary.
        return {"_keys": len(value)}
    return sweep_string(str(value))


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
