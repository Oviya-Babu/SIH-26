"""Ephemeral scoped tokens for the kiosk tier (CLAUDE.md §4, §8, §9, §33, §34).

Patients and caregivers are never Keycloak identities — a patient walks up with
zero prior enrolment (§1). They hold a short-lived, narrowly scoped token minted
by the backend after the *device* has authenticated with its provisioned
credential.

Three token kinds, deliberately non-interchangeable:

``kiosk``
    Issued to a provisioned device. Proves "this is tablet X in tenant Y,
    department Z". Carries no patient. A stolen, unprovisioned tablet cannot
    obtain one (§33).
``session``
    Issued after identity + consent. Scoped to exactly one ``session_id`` and one
    ``patient_id``.
``upload``
    Upload-only, one session, ~45 min TTL, for the QR-to-phone handoff. There is
    no read scope to escalate to, and it is rejected after the session closes
    (§9, §34).

Tokens are HMAC-signed compact JSON. A separate signing key per token kind means
a session token can never be replayed as an upload token or vice versa.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

TokenKind = Literal["kiosk", "session", "upload"]


class TokenError(Exception):
    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.message = message


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


@dataclass(frozen=True, slots=True)
class TokenClaims:
    kind: TokenKind
    tenant_id: UUID
    issued_at: int
    expires_at: int
    jti: str
    device_id: UUID | None = None
    department_id: UUID | None = None
    session_id: UUID | None = None
    patient_id: UUID | None = None
    # 'patient' | 'caregiver_respondent' | 'staff'
    subject_role: str = "patient"
    caregiver_auth_id: UUID | None = None
    respondent_relationship: str | None = None
    actor_id: UUID | None = None

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "k": self.kind,
            "t": str(self.tenant_id),
            "iat": self.issued_at,
            "exp": self.expires_at,
            "jti": self.jti,
            "r": self.subject_role,
        }
        for key, value in (
            ("dev", self.device_id),
            ("dep", self.department_id),
            ("sid", self.session_id),
            ("pid", self.patient_id),
            ("cga", self.caregiver_auth_id),
            ("act", self.actor_id),
        ):
            if value is not None:
                payload[key] = str(value)
        if self.respondent_relationship:
            payload["rel"] = self.respondent_relationship
        return payload

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> TokenClaims:
        def opt_uuid(key: str) -> UUID | None:
            raw = payload.get(key)
            return UUID(raw) if raw else None

        return cls(
            kind=payload["k"],
            tenant_id=UUID(payload["t"]),
            issued_at=int(payload["iat"]),
            expires_at=int(payload["exp"]),
            jti=str(payload["jti"]),
            device_id=opt_uuid("dev"),
            department_id=opt_uuid("dep"),
            session_id=opt_uuid("sid"),
            patient_id=opt_uuid("pid"),
            subject_role=str(payload.get("r", "patient")),
            caregiver_auth_id=opt_uuid("cga"),
            respondent_relationship=payload.get("rel"),
            actor_id=opt_uuid("act"),
        )


class TokenService:
    def __init__(self, secret: str) -> None:
        if not secret:
            raise ValueError("token secret must not be empty")
        self._root = secret.encode("utf-8")

    def _key(self, kind: TokenKind) -> bytes:
        # Domain-separated subkeys: cross-kind replay is cryptographically dead.
        return hmac.new(self._root, f"medikiosk:token:{kind}".encode(), hashlib.sha256).digest()

    def mint(
        self,
        kind: TokenKind,
        *,
        tenant_id: UUID,
        ttl_seconds: int,
        **fields: Any,
    ) -> tuple[str, TokenClaims]:
        now = int(time.time())
        claims = TokenClaims(
            kind=kind,
            tenant_id=tenant_id,
            issued_at=now,
            expires_at=now + ttl_seconds,
            jti=secrets.token_urlsafe(12),
            **fields,
        )
        body = _b64e(
            json.dumps(claims.to_payload(), separators=(",", ":"), sort_keys=True).encode()
        )
        signature = _b64e(hmac.new(self._key(kind), body.encode(), hashlib.sha256).digest())
        return f"{body}.{signature}", claims

    def verify(self, token: str, *, expect: TokenKind) -> TokenClaims:
        try:
            body, signature = token.split(".", 1)
        except ValueError as exc:
            raise TokenError("malformed_token", "token is malformed") from exc

        expected_sig = _b64e(hmac.new(self._key(expect), body.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(signature, expected_sig):
            # Also fails when a token of a different kind is presented, because
            # each kind uses a different subkey.
            raise TokenError("invalid_signature", "token signature is invalid")

        try:
            payload = json.loads(_b64d(body))
            claims = TokenClaims.from_payload(payload)
        except (ValueError, KeyError) as exc:
            raise TokenError("malformed_token", "token payload is malformed") from exc

        if claims.kind != expect:
            raise TokenError("wrong_token_kind", f"expected a {expect} token")
        if claims.expires_at <= int(time.time()):
            raise TokenError("token_expired", "token has expired")
        return claims

    @staticmethod
    def hash_token(token: str) -> str:
        """Server-side handle for a token, so the token itself is never stored."""
        return hashlib.sha256(token.encode("utf-8")).hexdigest()


def hash_device_credential(credential: str) -> str:
    """Device provisioning secrets are stored only as a digest (§8, §33)."""
    return hashlib.sha256(credential.encode("utf-8")).hexdigest()


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a, b)
