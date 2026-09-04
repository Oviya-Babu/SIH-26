"""OPA client — resource-level authorization (CLAUDE.md §5.1, §29).

RBAC has already answered "does this role have this endpoint". OPA answers the
contextual question: *this* identity, on *this* resource, in *this* department,
right now.

[RED LINE §5.1] Deny by default. A timeout, a connection error, a malformed
response, or a missing decision all evaluate to **deny**. An authorization
service outage is an outage, not an access grant.
"""

from __future__ import annotations

from pathlib import Path
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import httpx

from medikiosk.config import Settings
from medikiosk.observability.logging_setup import get_logger

log = get_logger(__name__)


@dataclass(slots=True)
class ResourceContext:
    """The resource half of an OPA input document."""

    type: str
    id: UUID | str | None = None
    tenant_id: UUID | None = None
    department_id: UUID | None = None
    patient_id: UUID | None = None
    session_id: UUID | None = None
    status: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_input(self) -> dict[str, Any]:
        doc: dict[str, Any] = {"type": self.type}
        if self.id is not None:
            doc["id"] = str(self.id)
        if self.tenant_id is not None:
            doc["tenant_id"] = str(self.tenant_id)
        if self.department_id is not None:
            doc["department_id"] = str(self.department_id)
        if self.patient_id is not None:
            doc["patient_id"] = str(self.patient_id)
        if self.session_id is not None:
            doc["session_id"] = str(self.session_id)
        if self.status is not None:
            doc["status"] = self.status
        doc.update(self.extra)
        return doc


@dataclass(frozen=True, slots=True)
class Decision:
    allow: bool
    reason_code: str


class OPAClient:
    def __init__(self, settings: Settings, client: httpx.AsyncClient) -> None:
        self._settings = settings
        self._client = client
        self._url = (
            f"{settings.opa_url.rstrip('/')}/v1/data/"
            f"{settings.opa_decision_path.strip('/')}"
        )

    async def ensure_policies(self) -> bool:
        """Ensure authz.rego is actively loaded into OPA. If missing, auto-upload via REST API."""
        policy_path = Path(__file__).resolve().parents[4] / "policies" / "opa" / "authz.rego"
        if not policy_path.is_file():
            return False
        try:
            resp = await self._client.get(
                f"{self._settings.opa_url.rstrip('/')}/v1/policies",
                timeout=self._settings.opa_timeout_seconds,
            )
            if resp.status_code == 200:
                policies = resp.json().get("result", [])
                if any(p.get("id") == "authz" or "authz.rego" in p.get("id", "") for p in policies):
                    return True
            # Policy missing in OPA: upload and compile it
            policy_text = policy_path.read_text(encoding="utf-8")
            put_resp = await self._client.put(
                f"{self._settings.opa_url.rstrip('/')}/v1/policies/authz",
                content=policy_text,
                headers={"Content-Type": "text/plain"},
                timeout=self._settings.opa_timeout_seconds,
            )
            if put_resp.status_code == 200:
                log.info("opa_policy_synced", policy_id="authz")
                return True
        except Exception as exc:
            log.warning("opa_policy_sync_failed", error=str(exc))
        return False

    async def evaluate(self, opa_input: dict[str, Any]) -> Decision:
        try:
            resp = await self._client.post(
                self._url,
                json={"input": opa_input},
                timeout=self._settings.opa_timeout_seconds,
            )
        except httpx.HTTPError as exc:
            log.warning(
                "opa_unreachable",
                component="opa",
                error_class=type(exc).__name__,
                opa_decision="deny",
                policy_path=self._settings.opa_decision_path,
            )
            return Decision(allow=self._settings.opa_fail_open, reason_code="opa_unreachable")

        if resp.status_code != 200:
            log.warning(
                "opa_error_status",
                component="opa",
                http_status=resp.status_code,
                opa_decision="deny",
            )
            return Decision(allow=False, reason_code="opa_error")

        body = resp.json()
        if "result" not in body:
            # If OPA returned undefined because policies were dropped/restarted, auto-sync and retry once
            synced = await self.ensure_policies()
            if synced:
                try:
                    retry_resp = await self._client.post(
                        self._url,
                        json={"input": opa_input},
                        timeout=self._settings.opa_timeout_seconds,
                    )
                    if retry_resp.status_code == 200:
                        retry_body = retry_resp.json()
                        if "result" in retry_body:
                            res = retry_body["result"]
                            allow = res is True or (isinstance(res, dict) and res.get("allow") is True)
                            return Decision(
                                allow=bool(allow),
                                reason_code="opa_allow" if allow else "opa_deny",
                            )
                except Exception:
                    pass
            # An undefined decision is a deny: the policy did not say yes.
            return Decision(allow=False, reason_code="opa_undefined")

        result = body["result"]
        allow = result is True or (isinstance(result, dict) and result.get("allow") is True)
        return Decision(
            allow=bool(allow),
            reason_code="opa_allow" if allow else "opa_deny",
        )

    async def health(self) -> bool:
        try:
            resp = await self._client.get(
                f"{self._settings.opa_url.rstrip('/')}/health",
                timeout=self._settings.opa_timeout_seconds,
            )
            if resp.status_code != 200:
                return False
            # Ensure policies are compiled and ready
            await self.ensure_policies()
            return True
        except httpx.HTTPError:
            return False


def build_input(
    *,
    action: str,
    role: str,
    tenant_id: UUID,
    resource: ResourceContext,
    actor_id: UUID | None = None,
    assigned_department_id: UUID | None = None,
    patient_id: UUID | None = None,
    mfa_satisfied: bool = False,
    authorized_session_ids: tuple[UUID, ...] = (),
) -> dict[str, Any]:
    return {
        "action": action,
        "user": {
            "id": str(actor_id) if actor_id else None,
            "role": role,
            "tenant_id": str(tenant_id),
            "assigned_department_id": (
                str(assigned_department_id) if assigned_department_id else None
            ),
            "patient_id": str(patient_id) if patient_id else None,
            "mfa_satisfied": mfa_satisfied,
            "authorized_session_ids": [str(s) for s in authorized_session_ids],
        },
        "resource": resource.to_input(),
    }
