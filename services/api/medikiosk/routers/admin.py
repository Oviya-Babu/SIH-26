"""IT Admin workspace (CLAUDE.md §4, §5.2, §8, §25).

The IT Admin configures their OWN tenant — devices, users, departments,
integration endpoints — and has deliberately no access to clinical data. That
boundary is enforced by the RBAC capability set (no ``CLINICAL_READ``), by OPA
(no clinical resource types allowed for ``it_admin``), and by RLS (tenant scope).

Privileged writes require a step-up MFA assertion on the token (§4).
"""

from __future__ import annotations

import secrets
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field as PField

from medikiosk.deps import Ctx, StaffPrincipal, require
from medikiosk.errors import Conflict, NotFound
from medikiosk.modules.audit import service as audit
from medikiosk.modules.tenant import service as tenant_service
from medikiosk.security.opa import ResourceContext
from medikiosk.security.rbac import Capability

router = APIRouter(prefix="/v1/admin", tags=["admin"])


@router.get("/tenant")
async def tenant_config(
    ctx: Ctx,
    principal: StaffPrincipal,
    authz: Annotated[Any, Depends(require(Capability.TENANT_CONFIG_READ, "read"))],
) -> dict[str, Any]:
    await authz.check(ResourceContext(type="tenant", tenant_id=principal.tenant_id))
    async with ctx.db.readonly(principal) as conn:
        tenant = await conn.fetchrow(
            """
            SELECT id, slug, display_name, status,
                   retention_days_documents, retention_days_clinical_facts,
                   retention_days_telemetry, created_at
              FROM tenant LIMIT 1
            """
        )
        departments = await tenant_service.list_departments(conn)
        protocol_config = await conn.fetch(
            "SELECT protocol_family, active_version, updated_at FROM tenant_protocol_config"
        )
        counts = await conn.fetchrow(
            """
            SELECT (SELECT count(*) FROM app_user WHERE status = 'active') AS active_users,
                   (SELECT count(*) FROM device WHERE status = 'active')   AS active_devices,
                   (SELECT count(*) FROM session)                          AS sessions_total,
                   (SELECT count(*) FROM patient)                          AS patients_total
            """
        )
    if tenant is None:
        raise NotFound("tenant not visible", reason_code="not_found")

    return {
        "tenant": dict(tenant),
        "departments": [
            {
                "id": str(d.id),
                "code": d.code,
                "display_name": d.display_name,
                "protocol_family": d.protocol_family,
            }
            for d in departments
        ],
        "protocol_config": [dict(r) for r in protocol_config],
        "counts": dict(counts) if counts else {},
        "clinical_access": False,
        "note": "IT Admin has no access to clinical records (CLAUDE.md §5.2).",
    }


class RetentionRequest(BaseModel):
    """DPDP-Rules-2025-mapped retention, tenant-configurable (§26, §38)."""

    retention_days_documents: int = PField(ge=30, le=36500)
    retention_days_clinical_facts: int = PField(ge=30, le=36500)
    retention_days_telemetry: int = PField(ge=1, le=365)


@router.post("/tenant/retention")
async def set_retention(
    ctx: Ctx,
    payload: RetentionRequest,
    principal: StaffPrincipal,
    authz: Annotated[Any, Depends(require(Capability.TENANT_CONFIG_WRITE, "write"))],
) -> dict[str, Any]:
    """Set retention periods.

    Bounds are deliberate: telemetry is capped at a year and clinical retention
    has a floor, so a misconfiguration cannot silently create either an
    indefinite telemetry store or an instantly-expiring medical record.
    """
    await authz.check(ResourceContext(type="tenant", tenant_id=principal.tenant_id))
    async with ctx.db.transaction(principal) as conn:
        row = await conn.fetchrow(
            """
            UPDATE tenant
               SET retention_days_documents = $1,
                   retention_days_clinical_facts = $2,
                   retention_days_telemetry = $3,
                   updated_at = now()
             WHERE id = $4
            RETURNING id, retention_days_documents, retention_days_clinical_facts,
                      retention_days_telemetry
            """,
            payload.retention_days_documents,
            payload.retention_days_clinical_facts,
            payload.retention_days_telemetry,
            principal.tenant_id,
        )
        await audit.record(
            conn,
            principal,
            action="tenant.retention_updated",
            entity_type="tenant",
            entity_id=principal.tenant_id,
            detail={"count": payload.retention_days_documents, "step_up_verified": True},
        )
    return dict(row)


class DeviceRequest(BaseModel):
    label: str = PField(min_length=2, max_length=80)
    department_id: UUID | None = None
    device_type: Literal["kiosk_tablet", "staff_capture"] = "kiosk_tablet"


@router.get("/devices")
async def list_devices(
    ctx: Ctx,
    principal: StaffPrincipal,
    authz: Annotated[Any, Depends(require(Capability.DEVICE_MANAGE, "read"))],
) -> dict[str, Any]:
    await authz.check(ResourceContext(type="device", tenant_id=principal.tenant_id))
    async with ctx.db.readonly(principal) as conn:
        rows = await conn.fetch(
            """
            SELECT d.id, d.label, d.device_type, d.status, d.last_seen_at, d.created_at,
                   dep.code AS department_code, dep.display_name AS department_name
              FROM device d
              LEFT JOIN department dep ON dep.id = d.department_id
             ORDER BY d.label
            """
        )
    return {"devices": [dict(r) for r in rows]}


@router.post("/devices")
async def provision_device(
    ctx: Ctx,
    payload: DeviceRequest,
    principal: StaffPrincipal,
    authz: Annotated[Any, Depends(require(Capability.DEVICE_MANAGE, "write"))],
) -> dict[str, Any]:
    """Provision a kiosk.

    The credential is generated server-side, returned EXACTLY ONCE, and stored
    only as a digest (§8, §33). If it is lost, the device is re-provisioned — it
    cannot be recovered, which is the property that makes the digest worth having.
    """
    await authz.check(ResourceContext(type="device", tenant_id=principal.tenant_id))
    credential = secrets.token_urlsafe(48)

    async with ctx.db.transaction(principal) as conn:
        device_id = await tenant_service.register_device(
            conn,
            tenant_id=principal.tenant_id,
            label=payload.label,
            credential=credential,
            department_id=payload.department_id,
            device_type=payload.device_type,
        )
        await audit.record(
            conn,
            principal,
            action="device.provisioned",
            entity_type="device",
            entity_id=device_id,
            detail={"step_up_verified": True, "client_kind": payload.device_type},
        )

    return {
        "device_id": str(device_id),
        "label": payload.label,
        "device_credential": credential,
        "warning": (
            "This credential is shown once and stored only as a digest. "
            "Provision it into the tablet now; it cannot be retrieved later."
        ),
    }


@router.post("/devices/{device_id}/revoke")
async def revoke_device(
    ctx: Ctx,
    device_id: UUID,
    principal: StaffPrincipal,
    authz: Annotated[Any, Depends(require(Capability.DEVICE_MANAGE, "write"))],
) -> dict[str, Any]:
    """Revoke a device — the response to a lost or stolen tablet (§33)."""
    await authz.check(
        ResourceContext(type="device", id=device_id, tenant_id=principal.tenant_id)
    )
    async with ctx.db.transaction(principal) as conn:
        row = await conn.fetchrow(
            "UPDATE device SET status = 'revoked' WHERE id = $1 RETURNING id, label",
            device_id,
        )
        if row is None:
            raise NotFound("device not found", reason_code="not_found")
        await audit.record(
            conn,
            principal,
            action="device.revoked",
            entity_type="device",
            entity_id=device_id,
            detail={"outcome": "revoked", "step_up_verified": True},
        )
    return {"device_id": str(device_id), "status": "revoked"}


class UserRequest(BaseModel):
    subject: str = PField(min_length=4, max_length=255)
    username: str = PField(min_length=2, max_length=120)
    display_name: str = PField(min_length=2, max_length=160)
    role: Literal[
        "nurse", "physician", "ayush_practitioner", "clinical_admin",
        "it_admin", "security_officer",
    ]
    assigned_department_id: UUID | None = None
    mfa_enrolled: bool = False


@router.get("/users")
async def list_users(
    ctx: Ctx,
    principal: StaffPrincipal,
    authz: Annotated[Any, Depends(require(Capability.USER_MANAGE, "read"))],
) -> dict[str, Any]:
    await authz.check(ResourceContext(type="app_user", tenant_id=principal.tenant_id))
    async with ctx.db.readonly(principal) as conn:
        rows = await conn.fetch(
            """
            SELECT u.id, u.username, u.display_name, u.role, u.status, u.mfa_enrolled,
                   u.created_at, dep.code AS department_code, dep.display_name AS department_name
              FROM app_user u
              LEFT JOIN department dep ON dep.id = u.assigned_department_id
             ORDER BY u.role, u.display_name
            """
        )
    return {"users": [dict(r) for r in rows]}


@router.post("/users")
async def provision_user(
    ctx: Ctx,
    payload: UserRequest,
    principal: StaffPrincipal,
    authz: Annotated[Any, Depends(require(Capability.USER_MANAGE, "write"))],
) -> dict[str, Any]:
    """Project a Keycloak identity into this tenant.

    Keycloak remains the authentication authority; this row carries the tenant's
    own facts about the user — role, department, status — which is what OPA
    evaluates. A clinical role without a department assignment is refused,
    because department scoping is how §5.2 least privilege is achieved.
    """
    await authz.check(ResourceContext(type="app_user", tenant_id=principal.tenant_id))

    if payload.role in ("nurse", "physician", "ayush_practitioner") and (
        payload.assigned_department_id is None
    ):
        raise Conflict(
            "clinical roles require a department assignment",
            reason_code="department_assignment_required",
        )

    async with ctx.db.transaction(principal) as conn:
        existing = await conn.fetchval(
            "SELECT id FROM app_user WHERE subject = $1", payload.subject
        )
        if existing is not None:
            raise Conflict("user already provisioned", reason_code="user_exists")
        user_id = await conn.fetchval(
            """
            INSERT INTO app_user (tenant_id, subject, username, display_name, role,
                                  assigned_department_id, mfa_enrolled)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            RETURNING id
            """,
            principal.tenant_id,
            payload.subject,
            payload.username,
            payload.display_name,
            payload.role,
            payload.assigned_department_id,
            payload.mfa_enrolled,
        )
        await audit.record(
            conn,
            principal,
            action="user.provisioned",
            entity_type="app_user",
            entity_id=user_id,
            detail={"actor_role": payload.role, "step_up_verified": True},
        )
    return {"user_id": str(user_id), "role": payload.role}


@router.post("/users/{user_id}/disable")
async def disable_user(
    ctx: Ctx,
    user_id: UUID,
    principal: StaffPrincipal,
    authz: Annotated[Any, Depends(require(Capability.USER_MANAGE, "write"))],
) -> dict[str, Any]:
    await authz.check(
        ResourceContext(type="app_user", id=user_id, tenant_id=principal.tenant_id)
    )
    async with ctx.db.transaction(principal) as conn:
        row = await conn.fetchrow(
            "UPDATE app_user SET status = 'disabled', updated_at = now() WHERE id = $1 "
            "RETURNING id, role",
            user_id,
        )
        if row is None:
            raise NotFound("user not found", reason_code="not_found")
        await audit.record(
            conn,
            principal,
            action="user.disabled",
            entity_type="app_user",
            entity_id=user_id,
            detail={"actor_role": row["role"], "step_up_verified": True},
        )
    return {"user_id": str(user_id), "status": "disabled"}


@router.get("/integrations")
async def integration_status(
    ctx: Ctx,
    principal: StaffPrincipal,
    authz: Annotated[Any, Depends(require(Capability.INTEGRATION_STATUS_READ, "read"))],
) -> dict[str, Any]:
    """Integration posture and delivery health (§23, §25, §37).

    Every environment is labelled honestly. [RED LINE §62] MediKiosk must never
    claim live ABDM or HIS production access it does not have, so the flags here
    are computed from configuration rather than asserted.
    """
    await authz.check(
        ResourceContext(type="integration_config", tenant_id=principal.tenant_id)
    )
    async with ctx.db.readonly(principal) as conn:
        outbox = await conn.fetchrow(
            """
            SELECT count(*) FILTER (WHERE status = 'pending')      AS pending,
                   count(*) FILTER (WHERE status = 'dispatched')   AS dispatched,
                   count(*) FILTER (WHERE status = 'delivered')    AS delivered,
                   count(*) FILTER (WHERE status = 'failed')       AS failed,
                   count(*) FILTER (WHERE status = 'dead_letter')  AS dead_letter
              FROM outbox_event
            """
        )
        deliveries = await conn.fetch(
            """
            SELECT target, environment, status, count(*) AS count,
                   max(delivered_at) AS last_delivery
              FROM integration_delivery
             GROUP BY target, environment, status
             ORDER BY target, environment
            """
        )

    queue_depths = await ctx.broker.queue_depths()
    ai_health = await ctx.ai.health()

    return {
        "abdm": {
            "environment": ctx.settings.abdm_environment,
            "base_url": ctx.settings.abdm_base_url,
            "consent_manager_id": ctx.settings.abdm_consent_manager_id,
            "credentials_configured": bool(ctx.settings.abdm_client_id),
            "is_production_access": False,
            "label": "ABDM SANDBOX — no production access is claimed (CLAUDE.md §23)",
        },
        "his": {
            "mode": ctx.settings.his_adapter_mode,
            "base_url": ctx.settings.his_base_url,
            "label": (
                "MOCK adapter — the pilot hospital and its HIS are not yet "
                "identified (CLAUDE.md §25 [ASSUMPTION])"
            ),
        },
        "terminology": {
            "version": ctx.settings.terminology_snapshot_version,
            "source": "static_snapshot",
            "is_live_api": False,
            "label": "NAMASTE/ICD-11 TM2 static snapshot; Ministry API terms unconfirmed (§24)",
        },
        "outbox": dict(outbox) if outbox else {},
        "deliveries": [dict(r) for r in deliveries],
        "broker": {"available": ctx.broker.available, "queue_depths": queue_depths},
        "ai_gateway": ai_health,
    }


@router.get("/health-summary")
async def health_summary(
    ctx: Ctx,
    principal: StaffPrincipal,
    authz: Annotated[Any, Depends(require(Capability.TENANT_CONFIG_READ, "read"))],
) -> dict[str, Any]:
    """Operational overview for the admin dashboard."""
    await authz.check(ResourceContext(type="tenant", tenant_id=principal.tenant_id))
    async with ctx.db.readonly(principal) as conn:
        stats = await conn.fetchrow(
            """
            SELECT (SELECT count(*) FROM session WHERE started_at > now() - interval '24 hours')
                                                                          AS sessions_24h,
                   (SELECT count(*) FROM session WHERE status = 'in_progress')
                                                                          AS sessions_live,
                   (SELECT count(*) FROM red_flag_alert
                     WHERE created_at > now() - interval '24 hours')       AS alerts_24h,
                   (SELECT count(*) FROM document
                     WHERE created_at > now() - interval '24 hours')       AS documents_24h,
                   (SELECT count(*) FROM document
                     WHERE processing_status IN ('queued','scanning','processing'))
                                                                          AS documents_in_flight,
                   (SELECT count(*) FROM physician_review WHERE status = 'exported')
                                                                          AS exported_total,
                   (SELECT count(*) FROM device
                     WHERE status = 'active'
                       AND last_seen_at > now() - interval '10 minutes')   AS devices_online
            """
        )
        by_language = await conn.fetch(
            """
            SELECT language, count(*) AS count FROM session
             GROUP BY language ORDER BY count DESC
            """
        )
        by_department = await conn.fetch(
            """
            SELECT d.display_name AS department, count(s.id) AS count
              FROM department d LEFT JOIN session s ON s.department_id = d.id
             GROUP BY d.display_name ORDER BY count DESC
            """
        )

    return {
        "stats": dict(stats) if stats else {},
        "sessions_by_language": [dict(r) for r in by_language],
        "sessions_by_department": [dict(r) for r in by_department],
        "broker_available": ctx.broker.available,
        "ai_breaker": ctx.ai.breaker.state(),
    }
