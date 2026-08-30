"""Triage — persisting red-flag evaluations and driving escalation (§14, §50).

Evaluation runs in the SAME TRANSACTION as the answer that triggered it: a
patient's answer and the safety decision about it are one atomic act. Nothing
about escalation is queued, because §50's rule for using the broker at all —
long-running, retryable or fan-out — does not apply, and [RED LINE §50] the
interactive patient loop never touches RabbitMQ.

Nurse notification is a WebSocket push (§50), not a queue message, so it arrives
inside the SLA rather than whenever a consumer happens to poll.

[RED LINE §14] MediKiosk does not integrate with or reorder any hospital's
physical token/queue system. A nurse escalates through whatever process the
hospital already uses; this module's job ends at telling them, in time.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import asyncpg

from medikiosk.db import Principal, to_jsonb
from medikiosk.errors import Conflict, NotFound
from medikiosk.modules.audit import service as audit
from medikiosk.modules.clinical_protocol.model import Protocol
from medikiosk.modules.triage.red_flag_engine import EngineResult, RuleSet, evaluate
from medikiosk.observability.logging_setup import get_logger

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class AlertNotification:
    """The staff-facing payload. Never rendered on the kiosk (§14)."""

    alert_id: UUID
    session_id: UUID
    department_id: UUID
    rule_id: str
    rule_name: str
    severity: str
    staff_message: str
    sla_seconds: int
    created_at: str
    patient_display: str
    completeness: float


class AlertHub:
    """In-process fan-out of red-flag alerts to connected nurse consoles.

    One process, one hub. When the deployment grows past a single API container
    this becomes a Redis pub/sub fan-out — the interface here is what makes that
    a transport change rather than a domain change (§42–45).
    """

    def __init__(self) -> None:
        self._subscribers: dict[tuple[UUID, UUID], set[asyncio.Queue]] = defaultdict(set)
        self._lock = asyncio.Lock()

    async def subscribe(self, tenant_id: UUID, department_id: UUID) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=64)
        async with self._lock:
            self._subscribers[(tenant_id, department_id)].add(queue)
        return queue

    async def unsubscribe(
        self, tenant_id: UUID, department_id: UUID, queue: asyncio.Queue
    ) -> None:
        async with self._lock:
            self._subscribers[(tenant_id, department_id)].discard(queue)

    async def publish(
        self, tenant_id: UUID, department_id: UUID, message: dict[str, Any]
    ) -> int:
        """Deliver to department-scoped subscribers only.

        Department scoping is enforced here as well as by OPA, so a bug in the
        console cannot subscribe a nurse to another department's alerts.
        """
        async with self._lock:
            queues = list(self._subscribers.get((tenant_id, department_id), ()))
        delivered = 0
        for queue in queues:
            try:
                queue.put_nowait(message)
                delivered += 1
            except asyncio.QueueFull:
                # A stalled console must never slow the patient's transaction.
                log.warning("alert_subscriber_lagging", component="triage")
        return delivered

    def subscriber_count(self, tenant_id: UUID, department_id: UUID) -> int:
        return len(self._subscribers.get((tenant_id, department_id), ()))


HUB = AlertHub()


async def evaluate_and_persist(
    conn: asyncpg.Connection,
    principal: Principal,
    *,
    ruleset: RuleSet,
    protocol: Protocol,
    session_id: UUID,
    department_id: UUID,
    answers: dict[str, Any],
    trigger_field_id: str | None,
) -> EngineResult:
    """Run the ruleset and persist EVERY evaluation, fired or not (§14)."""
    result = evaluate(ruleset, protocol, answers)

    # Persist all evaluations. This is what makes false-positive and
    # false-negative rates measurable later, instead of merely asserted.
    await conn.executemany(
        """
        INSERT INTO red_flag_evaluation
            (tenant_id, session_id, ruleset_version, rule_id, fired,
             evaluated_state, trigger_field_id)
        VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7)
        """,
        [
            (
                principal.tenant_id,
                session_id,
                result.ruleset_version,
                e.rule_id,
                e.fired,
                to_jsonb(e.evaluated_state),
                trigger_field_id,
            )
            for e in result.evaluations
        ],
    )

    for evaluation in result.alerts:
        # ON CONFLICT DO NOTHING: a rule that stays true across several answers
        # must not create a new alert each time, or the queue floods.
        alert_id = await conn.fetchval(
            """
            INSERT INTO red_flag_alert
                (tenant_id, session_id, department_id, rule_id, ruleset_version,
                 rule_name, severity, staff_message, sla_seconds)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            ON CONFLICT (session_id, rule_id) DO NOTHING
            RETURNING id
            """,
            principal.tenant_id,
            session_id,
            department_id,
            evaluation.rule_id,
            result.ruleset_version,
            evaluation.rule_name,
            str(evaluation.severity),
            evaluation.staff_rationale,
            evaluation.sla_seconds,
        )
        if alert_id is None:
            continue

        await audit.record(
            conn,
            principal,
            action="red_flag.alert_created",
            entity_type="red_flag_alert",
            entity_id=alert_id,
            detail={
                "rule_id": evaluation.rule_id,
                "ruleset_version": result.ruleset_version,
                "severity": str(evaluation.severity),
                "field_id": trigger_field_id,
            },
        )
        log.info(
            "red_flag_fired",
            component="triage",
            rule_id=evaluation.rule_id,
            ruleset_version=result.ruleset_version,
            severity=str(evaluation.severity),
            fired=True,
            session_id=session_id,
            tenant_id=principal.tenant_id,
            sla_seconds=evaluation.sla_seconds,
        )

    return result


async def notify_new_alerts(
    conn: asyncpg.Connection,
    *,
    tenant_id: UUID,
    session_id: UUID,
    department_id: UUID,
) -> int:
    """Push open alerts for a session to the nurse console.

    Called AFTER the transaction commits. Pushing before commit could announce
    an alert that then rolls back — a nurse arriving for a patient whose answer
    was never recorded.
    """
    rows = await conn.fetch(
        """
        SELECT a.id, a.session_id, a.department_id, a.rule_id, a.rule_name, a.severity,
               a.staff_message, a.sla_seconds, a.created_at,
               s.completeness,
               p.full_name, p.hospital_local_id
          FROM red_flag_alert a
          JOIN session s ON s.id = a.session_id
          JOIN patient p ON p.id = s.patient_id
         WHERE a.session_id = $1 AND a.status = 'open'
         ORDER BY a.created_at
        """,
        session_id,
    )
    delivered = 0
    for row in rows:
        payload = {
            "type": "red_flag_alert",
            "alert_id": str(row["id"]),
            "session_id": str(row["session_id"]),
            "rule_id": row["rule_id"],
            "rule_name": row["rule_name"],
            "severity": row["severity"],
            "staff_message": row["staff_message"],
            "sla_seconds": row["sla_seconds"],
            "created_at": row["created_at"].isoformat(),
            "patient_display": _patient_display(row),
            "completeness": float(row["completeness"]),
        }
        delivered += await HUB.publish(tenant_id, department_id, payload)
    return delivered


def _patient_display(row) -> str:
    """Minimal identification for a nurse who must physically find the patient.

    A nurse walking into a waiting room needs a name; withholding it would make
    the alert useless. This is controlled-access PHI in the clinical UI, and it
    is never emitted to telemetry (§28).
    """
    local = row["hospital_local_id"] or ""
    return f"{row['full_name']} ({local})" if local else row["full_name"]


async def queue(
    conn: asyncpg.Connection,
    *,
    department_id: UUID | None,
    statuses: tuple[str, ...] = ("open", "acknowledged", "escalated"),
) -> list[dict[str, Any]]:
    """The nurse's live queue, department-scoped, most urgent first."""
    rows = await conn.fetch(
        """
        SELECT a.id, a.session_id, a.department_id, a.rule_id, a.rule_name, a.severity,
               a.staff_message, a.sla_seconds, a.status, a.created_at,
               a.acknowledged_at, a.escalated_at, a.acknowledged_by,
               s.status AS session_status, s.completeness, s.fast_path_active,
               s.language, d.display_name AS department_name,
               p.full_name, p.hospital_local_id
          FROM red_flag_alert a
          JOIN session s ON s.id = a.session_id
          JOIN department d ON d.id = a.department_id
          JOIN patient p ON p.id = s.patient_id
         WHERE a.status = ANY($1::text[])
           AND ($2::uuid IS NULL OR a.department_id = $2)
         ORDER BY CASE a.severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1 ELSE 2 END,
                  a.created_at
        """,
        list(statuses),
        department_id,
    )
    now = datetime.now(timezone.utc)
    out: list[dict[str, Any]] = []
    for row in rows:
        elapsed = int((now - row["created_at"]).total_seconds())
        out.append(
            {
                "alert_id": row["id"],
                "session_id": row["session_id"],
                "department_id": row["department_id"],
                "department_name": row["department_name"],
                "rule_id": row["rule_id"],
                "rule_name": row["rule_name"],
                "severity": row["severity"],
                "staff_message": row["staff_message"],
                "sla_seconds": row["sla_seconds"],
                "elapsed_seconds": elapsed,
                # Surfacing the breach rather than hiding it is the point: an
                # unacknowledged critical alert is the most important row on the
                # screen, and it must look like it.
                "sla_breached": row["status"] == "open" and elapsed > row["sla_seconds"],
                "status": row["status"],
                "acknowledged_at": row["acknowledged_at"],
                "session_status": row["session_status"],
                "fast_path_active": row["fast_path_active"],
                "completeness": float(row["completeness"]),
                "language": row["language"],
                "patient_display": _patient_display(row),
                "created_at": row["created_at"],
            }
        )
    return out


_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "open": ("acknowledged", "escalated", "resolved"),
    "acknowledged": ("escalated", "resolved"),
    "escalated": ("acknowledged", "resolved"),
    "resolved": (),
}


async def transition(
    conn: asyncpg.Connection,
    principal: Principal,
    *,
    alert_id: UUID,
    next_status: str,
    note: str | None = None,
) -> dict[str, Any]:
    """Acknowledge / escalate / resolve an alert."""
    current = await conn.fetchrow(
        "SELECT id, status, department_id, rule_id, severity FROM red_flag_alert WHERE id = $1",
        alert_id,
    )
    if current is None:
        raise NotFound("alert not found", reason_code="not_found")
    allowed = _TRANSITIONS.get(current["status"], ())
    if next_status not in allowed:
        raise Conflict(
            f"cannot move an alert from {current['status']} to {next_status}",
            reason_code="illegal_alert_transition",
        )

    row = await conn.fetchrow(
        """
        UPDATE red_flag_alert
           SET status           = $2,
               acknowledged_by  = CASE WHEN $2 = 'acknowledged' THEN $3 ELSE acknowledged_by END,
               acknowledged_at  = CASE WHEN $2 = 'acknowledged' THEN now() ELSE acknowledged_at END,
               escalated_at     = CASE WHEN $2 = 'escalated'    THEN now() ELSE escalated_at END,
               resolved_by      = CASE WHEN $2 = 'resolved'     THEN $3 ELSE resolved_by END,
               resolved_at      = CASE WHEN $2 = 'resolved'     THEN now() ELSE resolved_at END,
               resolution_note  = COALESCE($4, resolution_note)
         WHERE id = $1
        RETURNING id, status, session_id, department_id, acknowledged_at
        """,
        alert_id,
        next_status,
        principal.actor_id,
        (note or None),
    )

    await audit.record(
        conn,
        principal,
        action=f"red_flag.alert_{next_status}",
        entity_type="red_flag_alert",
        entity_id=alert_id,
        detail={
            "previous_status": current["status"],
            "next_status": next_status,
            "rule_id": current["rule_id"],
            "severity": current["severity"],
            "reason": (note or "")[:200] or None,
        },
    )
    return dict(row)


async def sla_breaches(conn: asyncpg.Connection, grace_seconds: int) -> list[dict[str, Any]]:
    """Open alerts past their SLA, for auto-escalation to next-tier staff (§14)."""
    rows = await conn.fetch(
        """
        SELECT id, session_id, department_id, rule_id, severity, sla_seconds, created_at
          FROM red_flag_alert
         WHERE status = 'open'
           AND created_at < now() - make_interval(secs => sla_seconds + $1)
         ORDER BY created_at
        """,
        grace_seconds,
    )
    return [dict(r) for r in rows]


async def session_alerts(
    conn: asyncpg.Connection, session_id: UUID
) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        """
        SELECT id, rule_id, rule_name, severity, staff_message, status,
               sla_seconds, created_at, acknowledged_at, resolved_at
          FROM red_flag_alert
         WHERE session_id = $1
         ORDER BY CASE severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1 ELSE 2 END,
                  created_at
        """,
        session_id,
    )
    return [dict(r) for r in rows]


async def evaluation_stats(conn: asyncpg.Connection) -> list[dict[str, Any]]:
    """Fire rates per rule — the measurement §14 exists to make possible."""
    rows = await conn.fetch(
        """
        SELECT rule_id,
               ruleset_version,
               count(*)                              AS evaluations,
               count(*) FILTER (WHERE fired)         AS fired,
               round(
                   100.0 * count(*) FILTER (WHERE fired) / GREATEST(count(*), 1), 2
               )                                     AS fire_rate_pct
          FROM red_flag_evaluation
         GROUP BY rule_id, ruleset_version
         ORDER BY fired DESC, rule_id
        """
    )
    return [dict(r) for r in rows]
