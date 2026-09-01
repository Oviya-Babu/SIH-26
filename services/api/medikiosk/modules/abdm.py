"""ABDM sandbox boundary (§7.2, §23).

MediKiosk is a HIP/health-information provider adapter here. The Consent
Manager owns network consent artifacts; this module may authenticate to the
configured sandbox and record an artifact reference returned by that manager,
but it never creates consent on the patient's behalf.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import asyncpg
import httpx

from medikiosk.config import Settings
from medikiosk.db import Principal
from medikiosk.errors import DependencyUnavailable, ValidationFailed
from medikiosk.modules.audit import service as audit


@dataclass(frozen=True, slots=True)
class SandboxStatus:
    environment: str
    base_url: str
    consent_manager_id: str
    credentials_configured: bool
    reachable: bool
    token_endpoint: str
    detail: str


class AbdmSandboxClient:
    """Small HTTP client for the configured ABDM gateway.

    Endpoint paths are settings so sandbox API revisions do not leak into the
    clinical core. No patient data is sent by the status check.
    """

    def __init__(self, settings: Settings, http: httpx.AsyncClient) -> None:
        self.settings = settings
        self.http = http
        self.base_url = settings.abdm_base_url.rstrip("/")
        self.token_endpoint = f"{self.base_url}{settings.abdm_token_path}"

    async def status(self) -> SandboxStatus:
        configured = bool(self.settings.abdm_client_id and self.settings.abdm_client_secret)
        if not configured:
            return SandboxStatus(
                environment=self.settings.abdm_environment,
                base_url=self.base_url,
                consent_manager_id=self.settings.abdm_consent_manager_id,
                credentials_configured=False,
                reachable=False,
                token_endpoint=self.token_endpoint,
                detail="ABDM sandbox credentials are not configured",
            )
        try:
            response = await self.http.post(
                self.token_endpoint,
                data={
                    "clientId": self.settings.abdm_client_id,
                    "clientSecret": self.settings.abdm_client_secret,
                    "grantType": "client_credentials",
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=self.settings.abdm_timeout_seconds,
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            return SandboxStatus(
                environment=self.settings.abdm_environment,
                base_url=self.base_url,
                consent_manager_id=self.settings.abdm_consent_manager_id,
                credentials_configured=True,
                reachable=False,
                token_endpoint=self.token_endpoint,
                detail=f"sandbox token exchange failed: {type(exc).__name__}",
            )
        return SandboxStatus(
            environment=self.settings.abdm_environment,
            base_url=self.base_url,
            consent_manager_id=self.settings.abdm_consent_manager_id,
            credentials_configured=True,
            reachable=True,
            token_endpoint=self.token_endpoint,
            detail="sandbox token exchange succeeded",
        )


def _artifact_status(value: str) -> str:
    allowed = {"requested", "granted", "denied", "revoked", "expired"}
    if value not in allowed:
        raise ValidationFailed("invalid ABDM artifact status", reason_code="abdm_status_invalid")
    return value


async def record_artifact_reference(
    conn: asyncpg.Connection,
    principal: Principal,
    *,
    patient_id: UUID,
    artifact_id: str,
    consent_manager_id: str,
    status: str,
    hiu_id: str | None,
    granted_at: datetime | None,
    expires_at: datetime | None,
    raw_artifact: dict[str, Any],
) -> UUID:
    """Record an artifact issued by the external Consent Manager.

    This endpoint stores a pointer and an auditable response envelope. It does
    not turn an internal consent grant into ABDM consent.
    """
    if not artifact_id.strip() or len(artifact_id) > 256:
        raise ValidationFailed("artifact_id is invalid", reason_code="abdm_artifact_invalid")
    if consent_manager_id != "sbx" and principal.tenant_id is None:
        raise ValidationFailed("consent manager is invalid", reason_code="abdm_cm_invalid")
    status = _artifact_status(status)
    if raw_artifact is None:
        raw_artifact = {}
    checksum = hashlib.sha256(
        json.dumps(raw_artifact, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    row = await conn.fetchrow(
        """
        INSERT INTO abdm_consent_artifact_ref
            (tenant_id, patient_id, artifact_id, consent_manager_id, environment,
             status, hiu_id, granted_at, expires_at, raw_artifact)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb)
        ON CONFLICT (tenant_id, artifact_id) DO UPDATE SET
            status = EXCLUDED.status,
            hiu_id = EXCLUDED.hiu_id,
            granted_at = EXCLUDED.granted_at,
            expires_at = EXCLUDED.expires_at,
            raw_artifact = EXCLUDED.raw_artifact
        RETURNING id
        """,
        principal.tenant_id,
        patient_id,
        artifact_id.strip(),
        consent_manager_id,
        "sandbox",
        status,
        hiu_id,
        granted_at,
        expires_at,
        json.dumps({"artifact": raw_artifact, "sha256": checksum}),
    )
    await audit.record(
        conn,
        principal,
        action="abdm.consent_artifact_recorded",
        entity_type="abdm_consent_artifact_ref",
        entity_id=row["id"],
        detail={"environment": "sandbox", "status": status, "consent_manager_id": consent_manager_id},
    )
    return row["id"]


async def patient_artifacts(
    conn: asyncpg.Connection, principal: Principal, patient_id: UUID
) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        """
        SELECT id, artifact_id, consent_manager_id, environment, status, hiu_id,
               granted_at, expires_at, created_at
          FROM abdm_consent_artifact_ref
         WHERE tenant_id = $1 AND patient_id = $2
         ORDER BY created_at DESC
        """,
        principal.tenant_id,
        patient_id,
    )
    return [dict(row) for row in rows]


def status_payload(status: SandboxStatus) -> dict[str, Any]:
    return {
        "environment": status.environment,
        "base_url": status.base_url,
        "consent_manager_id": status.consent_manager_id,
        "credentials_configured": status.credentials_configured,
        "reachable": status.reachable,
        "token_endpoint": status.token_endpoint,
        "detail": status.detail,
        "production_access_claimed": False,
        "label": "ABDM Sandbox" if status.environment == "sandbox" else "ABDM production configuration",
    }
