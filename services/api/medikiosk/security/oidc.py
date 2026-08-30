"""OIDC verification — the first link in the chain (CLAUDE.md §5.1).

    Request → TLS → OIDC (Keycloak: identity + tenant claim)
      → RBAC → OPA → app rule → RLS

This module answers only "who is this, and did Keycloak really say so?". It never
answers "may they do this" — that is RBAC (:mod:`medikiosk.security.rbac`) and
OPA (:mod:`medikiosk.security.opa`). Keeping them separate is what makes "no
layer trusts the layer above it" true rather than aspirational.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import httpx
from jose import jwt
from jose.exceptions import JWTError

from medikiosk.config import Settings
from medikiosk.observability.logging_setup import get_logger

log = get_logger(__name__)

# MediKiosk roles, exactly the seven of §5.2 (patient/caregiver are not Keycloak
# identities — they hold ephemeral kiosk tokens instead, §4).
STAFF_ROLES: frozenset[str] = frozenset(
    {
        "nurse",
        "physician",
        "ayush_practitioner",
        "clinical_admin",
        "it_admin",
        "security_officer",
    }
)


class AuthenticationError(Exception):
    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.message = message


@dataclass(frozen=True, slots=True)
class StaffClaims:
    subject: str
    tenant_id: UUID
    role: str
    username: str
    display_name: str
    mfa_satisfied: bool
    session_state: str | None
    expires_at: int


class JWKSCache:
    """Caches the issuer's JWKS. Refreshed on unknown kid, not on every call."""

    def __init__(self, settings: Settings, client: httpx.AsyncClient) -> None:
        self._settings = settings
        self._client = client
        self._keys: dict[str, dict[str, Any]] = {}
        self._fetched_at: float = 0.0
        self._issuer_meta: dict[str, Any] | None = None

    async def _discover(self) -> dict[str, Any]:
        if self._issuer_meta is None:
            url = f"{self._settings.oidc_issuer.rstrip('/')}/.well-known/openid-configuration"
            resp = await self._client.get(url, timeout=5.0)
            resp.raise_for_status()
            self._issuer_meta = resp.json()
        return self._issuer_meta

    async def _refresh(self) -> None:
        meta = await self._discover()
        resp = await self._client.get(meta["jwks_uri"], timeout=5.0)
        resp.raise_for_status()
        self._keys = {k["kid"]: k for k in resp.json().get("keys", []) if "kid" in k}
        self._fetched_at = time.monotonic()

    async def key_for(self, kid: str) -> dict[str, Any]:
        stale = (time.monotonic() - self._fetched_at) > self._settings.oidc_jwks_ttl_seconds
        if kid not in self._keys or stale:
            await self._refresh()
        key = self._keys.get(kid)
        if key is None:
            raise AuthenticationError("unknown_signing_key", "token signing key is unknown")
        return key


class OIDCVerifier:
    def __init__(self, settings: Settings, client: httpx.AsyncClient) -> None:
        self._settings = settings
        self._jwks = JWKSCache(settings, client)

    async def verify(self, token: str) -> StaffClaims:
        try:
            header = jwt.get_unverified_header(token)
        except JWTError as exc:
            raise AuthenticationError("malformed_token", "token is malformed") from exc

        kid = header.get("kid")
        if not kid:
            raise AuthenticationError("malformed_token", "token has no key id")

        key = await self._jwks.key_for(kid)

        try:
            claims = jwt.decode(
                token,
                key,
                algorithms=[header.get("alg", "RS256")],
                audience=self._settings.oidc_audience,
                issuer=self._settings.oidc_issuer,
                options={"verify_aud": True, "verify_iss": True, "verify_exp": True},
            )
        except JWTError as exc:
            raise AuthenticationError("invalid_token", "token verification failed") from exc

        return self._to_claims(claims)

    def _to_claims(self, claims: dict[str, Any]) -> StaffClaims:
        subject = claims.get("sub")
        if not subject:
            raise AuthenticationError("invalid_token", "token has no subject")

        # Tenant is a claim asserted by Keycloak, never a request parameter.
        raw_tenant = claims.get("tenant_id") or claims.get("https://medikiosk/tenant_id")
        if not raw_tenant:
            raise AuthenticationError("missing_tenant_claim", "token has no tenant claim")
        try:
            tenant_id = UUID(str(raw_tenant))
        except ValueError as exc:
            raise AuthenticationError("missing_tenant_claim", "tenant claim is not a uuid") from exc

        role = self._single_role(claims)
        mfa = self._mfa_satisfied(claims)

        if role in self._settings.mfa_required_roles and not mfa:
            # §27: MFA for Physician/Admin/Governance/Security. Enforced here, at
            # the edge, so no downstream handler has to remember.
            raise AuthenticationError(
                "mfa_required",
                f"role {role} requires multi-factor authentication",
            )

        return StaffClaims(
            subject=str(subject),
            tenant_id=tenant_id,
            role=role,
            username=str(claims.get("preferred_username") or subject),
            display_name=str(claims.get("name") or claims.get("preferred_username") or subject),
            mfa_satisfied=mfa,
            session_state=claims.get("session_state"),
            expires_at=int(claims.get("exp", 0)),
        )

    @staticmethod
    def _single_role(claims: dict[str, Any]) -> str:
        realm_roles = set(claims.get("realm_access", {}).get("roles", []))
        client_roles: set[str] = set()
        for entry in claims.get("resource_access", {}).values():
            client_roles.update(entry.get("roles", []))

        matched = sorted((realm_roles | client_roles) & STAFF_ROLES)
        if not matched:
            raise AuthenticationError("no_medikiosk_role", "token carries no MediKiosk role")
        if len(matched) > 1:
            # Least privilege: a multi-role token is ambiguous and we refuse to
            # guess which privilege set applies (§5).
            raise AuthenticationError(
                "ambiguous_role",
                "token carries multiple MediKiosk roles: " + ", ".join(matched),
            )
        return matched[0]

    @staticmethod
    def _mfa_satisfied(claims: dict[str, Any]) -> bool:
        acr = str(claims.get("acr", ""))
        amr = {str(v).lower() for v in claims.get("amr", []) or []}
        if acr in {"2", "mfa", "urn:mace:incommon:iap:silver"}:
            return True
        return bool(amr & {"mfa", "otp", "totp", "hwk", "swk"})
