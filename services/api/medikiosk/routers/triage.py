"""Nurse / Triage console API (CLAUDE.md §4, §14, §50).

The nurse surface is deliberately narrow: a department-scoped red-flag queue and
three actions on it. There is no physician action here and no admin action here —
that is the least-privilege boundary of §5.2, and it is enforced by RBAC
capabilities and OPA, not by hiding buttons.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field as PField

from medikiosk.deps import Ctx, StaffPrincipal, get_ctx, require, staff_principal
from medikiosk.errors import Forbidden, NotFound
from medikiosk.modules.session import service as session_service
from medikiosk.modules.triage import service as triage_service
from medikiosk.observability.logging_setup import get_logger
from medikiosk.security.opa import ResourceContext
from medikiosk.security.rbac import Capability

log = get_logger(__name__)

router = APIRouter(prefix="/v1/triage", tags=["triage"])


@router.get("/alerts")
async def list_alerts(
    ctx: Ctx,
    principal: StaffPrincipal,
    authz: Annotated[Any, Depends(require(Capability.TRIAGE_QUEUE_READ, "read"))],
    status: Annotated[list[str] | None, Query()] = None,
) -> dict[str, Any]:
    """The live red-flag queue, scoped to the nurse's own department (§5.2).

    ``department_id`` is taken from the authenticated user's assignment, never
    from a query parameter, so there is no parameter to tamper with.
    """
    if principal.department_id is None and principal.role == "nurse":
        raise Forbidden(
            "nurse account has no department assignment",
            reason_code="department_unassigned",
        )

    await authz.check(
        ResourceContext(
            type="red_flag_alert",
            tenant_id=principal.tenant_id,
            department_id=principal.department_id,
        )
    )

    statuses = tuple(status) if status else ("open", "acknowledged", "escalated")
    async with ctx.db.readonly(principal) as conn:
        alerts = await triage_service.queue(
            conn, department_id=principal.department_id, statuses=statuses
        )

    return {
        "department_id": str(principal.department_id) if principal.department_id else None,
        "counts": {
            "open": sum(1 for a in alerts if a["status"] == "open"),
            "critical_open": sum(
                1 for a in alerts if a["status"] == "open" and a["severity"] == "critical"
            ),
            "sla_breached": sum(1 for a in alerts if a["sla_breached"]),
        },
        "alerts": [_alert_payload(a) for a in alerts],
    }


class AlertActionRequest(BaseModel):
    note: str | None = PField(default=None, max_length=2000)


async def _load_alert(conn, alert_id: UUID) -> dict[str, Any]:
    row = await conn.fetchrow(
        """
        SELECT a.id, a.session_id, a.department_id, a.status, a.severity, a.rule_id,
               s.tenant_id, s.patient_id, pr.status AS review_status
          FROM red_flag_alert a
          JOIN session s ON s.id = a.session_id
          LEFT JOIN physician_review pr ON pr.session_id = s.id
         WHERE a.id = $1
        """,
        alert_id,
    )
    if row is None:
        raise NotFound("alert not found", reason_code="not_found")
    return dict(row)


def _alert_resource(row: dict[str, Any]) -> ResourceContext:
    return ResourceContext(
        type="red_flag_alert",
        id=row["id"],
        tenant_id=row["tenant_id"],
        department_id=row["department_id"],
        patient_id=row["patient_id"],
        status=row["status"],
        extra={"session_id": str(row["session_id"])},
    )


@router.post("/alerts/{alert_id}/ack")
async def acknowledge_alert(
    ctx: Ctx,
    alert_id: UUID,
    payload: AlertActionRequest,
    principal: StaffPrincipal,
    authz: Annotated[Any, Depends(require(Capability.TRIAGE_ALERT_ACK, "acknowledge"))],
) -> dict[str, Any]:
    """Acknowledge — 'I have seen this and I am going'.

    Stops the SLA clock for this alert and tells the kiosk so the patient sees a
    reassuring "a nurse has seen this and is on the way" (§14).
    """
    async with ctx.db.transaction(principal) as conn:
        row = await _load_alert(conn, alert_id)
        await authz.check(_alert_resource(row))
        result = await triage_service.transition(
            conn, principal, alert_id=alert_id, next_status="acknowledged", note=payload.note
        )
    return {"alert_id": str(alert_id), "status": result["status"]}


@router.post("/alerts/{alert_id}/escalate")
async def escalate_alert(
    ctx: Ctx,
    alert_id: UUID,
    payload: AlertActionRequest,
    principal: StaffPrincipal,
    authz: Annotated[Any, Depends(require(Capability.TRIAGE_ALERT_ESCALATE, "escalate"))],
) -> dict[str, Any]:
    """Escalate to next-tier staff.

    [RED LINE §14] Staff-mediated only. MediKiosk does not reorder the hospital's
    physical token or queue system; the nurse escalates through whatever process
    the hospital already has, and this records that they did.
    """
    async with ctx.db.transaction(principal) as conn:
        row = await _load_alert(conn, alert_id)
        await authz.check(_alert_resource(row))
        result = await triage_service.transition(
            conn, principal, alert_id=alert_id, next_status="escalated", note=payload.note
        )
    return {
        "alert_id": str(alert_id),
        "status": result["status"],
        "note": "escalation is staff-mediated; no hospital queue was reordered",
    }


@router.post("/alerts/{alert_id}/resolve")
async def resolve_alert(
    ctx: Ctx,
    alert_id: UUID,
    payload: AlertActionRequest,
    principal: StaffPrincipal,
    authz: Annotated[Any, Depends(require(Capability.TRIAGE_ALERT_RESOLVE, "resolve"))],
) -> dict[str, Any]:
    async with ctx.db.transaction(principal) as conn:
        row = await _load_alert(conn, alert_id)
        await authz.check(_alert_resource(row))
        result = await triage_service.transition(
            conn, principal, alert_id=alert_id, next_status="resolved", note=payload.note
        )
    return {"alert_id": str(alert_id), "status": result["status"]}


@router.get("/sessions")
async def department_sessions(
    ctx: Ctx,
    principal: StaffPrincipal,
    authz: Annotated[Any, Depends(require(Capability.SESSION_READ_DEPARTMENT, "read"))],
    status: Annotated[list[str] | None, Query()] = None,
) -> dict[str, Any]:
    """Live session status for the nurse's department — the station monitor."""
    await authz.check(
        ResourceContext(
            type="session",
            tenant_id=principal.tenant_id,
            department_id=principal.department_id,
        )
    )
    statuses = tuple(status) if status else (
        "in_progress", "escalated_to_staff", "awaiting_confirmation"
    )
    async with ctx.db.readonly(principal) as conn:
        rows = await conn.fetch(
            """
            SELECT s.id, s.status, s.completeness, s.language, s.fast_path_active,
                   s.started_at, s.last_activity_at, s.respondent_type,
                   s.protocol_family, dev.label AS device_label,
                   p.full_name, p.hospital_local_id,
                   (SELECT count(*) FROM red_flag_alert a
                     WHERE a.session_id = s.id AND a.status = 'open') AS open_alerts
              FROM session s
              JOIN patient p ON p.id = s.patient_id
              LEFT JOIN device dev ON dev.id = s.device_id
             WHERE s.status = ANY($1::text[])
               AND ($2::uuid IS NULL OR s.department_id = $2)
             ORDER BY s.fast_path_active DESC, s.last_activity_at DESC
            """,
            list(statuses),
            principal.department_id,
        )
    return {
        "sessions": [
            {
                "session_id": str(r["id"]),
                "status": r["status"],
                "completeness": float(r["completeness"]),
                "language": r["language"],
                "fast_path_active": r["fast_path_active"],
                "respondent_type": r["respondent_type"],
                "protocol_family": r["protocol_family"],
                "device_label": r["device_label"],
                "patient_display": (
                    f"{r['full_name']} ({r['hospital_local_id']})"
                    if r["hospital_local_id"]
                    else r["full_name"]
                ),
                "open_alerts": int(r["open_alerts"]),
                "started_at": r["started_at"],
                "last_activity_at": r["last_activity_at"],
            }
            for r in rows
        ]
    }


@router.get("/sessions/{session_id}/alerts")
async def session_alerts(
    ctx: Ctx,
    session_id: UUID,
    principal: StaffPrincipal,
    authz: Annotated[Any, Depends(require(Capability.TRIAGE_QUEUE_READ, "read"))],
) -> dict[str, Any]:
    async with ctx.db.readonly(principal) as conn:
        session = await session_service.get_snapshot(conn, session_id)
        await authz.check(
            ResourceContext(
                type="red_flag_alert",
                tenant_id=session.tenant_id,
                department_id=session.department_id,
                patient_id=session.patient_id,
            )
        )
        alerts = await triage_service.session_alerts(conn, session_id)
    return {"session_id": str(session_id), "alerts": alerts}


class StaffTakeoverRequest(BaseModel):
    outcome: Literal["patient_taken_to_treatment", "assessed_and_returned", "patient_left"]
    note: str | None = PField(default=None, max_length=2000)


@router.post("/sessions/{session_id}/takeover")
async def record_takeover(
    ctx: Ctx,
    session_id: UUID,
    payload: StaffTakeoverRequest,
    principal: StaffPrincipal,
    authz: Annotated[Any, Depends(require(Capability.TRIAGE_ALERT_RESOLVE, "resolve"))],
) -> dict[str, Any]:
    """Record the physical takeover after an escalation (§14.5).

    The kiosk interview has ended; a nurse has the patient. Recording the outcome
    closes the loop so the physician knows what happened between the alert and
    the consultation.
    """
    from medikiosk.modules.audit import service as audit

    async with ctx.db.transaction(principal) as conn:
        session = await session_service.get_snapshot(conn, session_id)
        await authz.check(
            ResourceContext(
                type="red_flag_alert",
                tenant_id=session.tenant_id,
                department_id=session.department_id,
                patient_id=session.patient_id,
                status=session.status,
            )
        )
        await audit.record(
            conn,
            principal,
            action="triage.staff_takeover_recorded",
            entity_type="session",
            entity_id=session_id,
            detail={"outcome": payload.outcome, "reason": (payload.note or "")[:200] or None},
        )
    return {"session_id": str(session_id), "outcome": payload.outcome}


# ---------------------------------------------------------------------------
# WebSocket push (§50: RedFlagFired is pushed, never queued)
# ---------------------------------------------------------------------------
@router.websocket("/stream")
async def alert_stream(websocket: WebSocket) -> None:
    """Real-time alert stream for the nurse console.

    The token arrives as a subprotocol/query parameter because browsers cannot
    set headers on a WebSocket handshake. It is verified through the SAME OIDC
    path as any REST call — the socket does not get a weaker check — and the
    subscription is department-scoped server-side.
    """
    ctx = get_ctx(websocket)  # type: ignore[arg-type]
    token = websocket.query_params.get("access_token", "")
    if not token:
        await websocket.close(code=4401)
        return

    try:
        claims = await ctx.oidc.verify(token)
    except Exception:  # noqa: BLE001
        await websocket.close(code=4401)
        return

    if claims.role not in ("nurse", "physician", "ayush_practitioner"):
        await websocket.close(code=4403)
        return

    from medikiosk.db import Principal

    bootstrap = Principal(tenant_id=claims.tenant_id, role=claims.role, subject=claims.subject)
    async with ctx.db.readonly(bootstrap) as conn:
        row = await conn.fetchrow(
            "SELECT id, assigned_department_id, status FROM app_user WHERE subject = $1",
            claims.subject,
        )
    if row is None or row["status"] != "active" or row["assigned_department_id"] is None:
        await websocket.close(code=4403)
        return

    department_id = row["assigned_department_id"]
    await websocket.accept()
    queue = await triage_service.HUB.subscribe(claims.tenant_id, department_id)
    log.info(
        "alert_stream_opened",
        component="triage",
        actor_role=claims.role,
        tenant_id=claims.tenant_id,
        queue_depth=triage_service.HUB.subscriber_count(claims.tenant_id, department_id),
    )

    try:
        # Send the current open queue immediately, so a console that connects
        # after an alert fired is not blind to it.
        async with ctx.db.readonly(bootstrap) as conn:
            backlog = await triage_service.queue(conn, department_id=department_id,
                                                 statuses=("open", "escalated"))
        await websocket.send_json({"type": "backlog", "alerts": [
            _alert_payload(a, serialise=True) for a in backlog
        ]})

        while True:
            try:
                message = await asyncio.wait_for(queue.get(), timeout=20.0)
            except asyncio.TimeoutError:
                # Heartbeat keeps proxies from dropping an idle socket, and lets
                # the console show "connected" honestly.
                await websocket.send_json({"type": "heartbeat"})
                continue
            await websocket.send_json(message)
    except WebSocketDisconnect:
        pass
    finally:
        await triage_service.HUB.unsubscribe(claims.tenant_id, department_id, queue)
        with contextlib.suppress(RuntimeError):
            await websocket.close()


def _alert_payload(alert: dict[str, Any], *, serialise: bool = False) -> dict[str, Any]:
    payload = {
        "alert_id": str(alert["alert_id"]),
        "session_id": str(alert["session_id"]),
        "rule_id": alert["rule_id"],
        "rule_name": alert["rule_name"],
        "severity": alert["severity"],
        "staff_message": alert["staff_message"],
        "sla_seconds": alert["sla_seconds"],
        "elapsed_seconds": alert["elapsed_seconds"],
        "sla_breached": alert["sla_breached"],
        "status": alert["status"],
        "session_status": alert["session_status"],
        "fast_path_active": alert["fast_path_active"],
        "completeness": alert["completeness"],
        "language": alert["language"],
        "patient_display": alert["patient_display"],
        "department_name": alert.get("department_name"),
    }
    payload["created_at"] = (
        alert["created_at"].isoformat() if serialise else alert["created_at"]
    )
    return payload


_ = staff_principal  # keep the dependency importable for tests
