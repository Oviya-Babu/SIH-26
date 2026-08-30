"""Audit — append-only, hash-chained, same-transaction (CLAUDE.md §31).

[RED LINE] Every module writes its own audit row **in the same transaction** as
the state change. That is why :func:`record` takes an already-open connection
rather than opening its own: it is structurally impossible to call it "later".
If the audit insert fails, the caller's transaction rolls back with it.

The ``detail`` payload is allowlist-filtered here, not at the logger, because an
audit row is durable: a PHI leak into ``audit_event.detail`` cannot be
retro-redacted on an append-only table (§28, §31).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import asyncpg

from medikiosk.db import Principal, to_jsonb

# Keys permitted in audit detail. Deliberately narrow: an audit row records
# *that* something happened and to which entity, not the clinical content.
_DETAIL_ALLOWLIST: frozenset[str] = frozenset(
    {
        "reason",
        "reason_code",
        "outcome",
        "previous_status",
        "next_status",
        "field_id",
        "category",
        "concept_code",
        "rule_id",
        "ruleset_version",
        "severity",
        "purpose",
        "purposes",
        "granted",
        "notice_version",
        "notice_language",
        "language",
        "grantor_type",
        "authority_basis",
        "relationship",
        "respondent_type",
        "input_method",
        "protocol_family",
        "protocol_version",
        "department_code",
        "capture_path",
        "verified_mime",
        "size_bytes",
        "pages",
        "malware_scan_status",
        "quality_status",
        "target",
        "environment",
        "idempotency_key",
        "statement_count",
        "citation_count",
        "generation_mode",
        "model_version",
        "confidence_band",
        "fact_count",
        "superseded_fact_id",
        "resulting_fact_id",
        "namaste_code",
        "icd11_tm2_code",
        "terminology_version",
        "completeness",
        "fast_path",
        "skip_reason",
        "count",
        "purged_keys",
        "step_up_verified",
        "client_kind",
        "http_status",
        "policy_path",
        "opa_decision",
        "chain_intact",
        "broken_at",
        "abnormal_flag",
        "conflict_id",
        "resolution",
    }
)


class AuditDetailError(ValueError):
    """Raised when a caller tries to persist non-allowlisted audit detail."""


def sanitise_detail(detail: dict[str, Any] | None) -> dict[str, Any]:
    if not detail:
        return {}
    rejected = sorted(set(detail) - _DETAIL_ALLOWLIST)
    if rejected:
        raise AuditDetailError(
            "audit detail keys not allowlisted: " + ", ".join(rejected)
        )
    return {k: _coerce(v) for k, v in detail.items() if v is not None}


def _coerce(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, (list, tuple)):
        return [_coerce(v) for v in value[:50]]
    return str(value)


async def record(
    conn: asyncpg.Connection,
    principal: Principal,
    *,
    action: str,
    entity_type: str,
    entity_id: UUID,
    detail: dict[str, Any] | None = None,
) -> int:
    """Append one audit row inside the caller's transaction.

    Returns the audit row id. ``prev_hash``/``row_hash`` are computed by the
    database trigger, so no application path can write an unchained row.
    """
    row = await conn.fetchrow(
        """
        INSERT INTO audit_event
            (tenant_id, actor_id, actor_role, action, entity_type, entity_id,
             detail, prev_hash, row_hash)
        VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, '', '')
        RETURNING id
        """,
        principal.tenant_id,
        principal.actor_id,
        principal.role,
        action,
        entity_type,
        entity_id,
        to_jsonb(sanitise_detail(detail)),
    )
    return int(row["id"])


async def verify_chain(conn: asyncpg.Connection) -> dict[str, Any]:
    """Verify the hash chain. Used by the Security console and CI (§9 DoD)."""
    rows = await conn.fetch("SELECT * FROM audit_chain_verify()")
    if not rows:
        total = await conn.fetchval("SELECT count(*) FROM audit_event")
        return {"chain_intact": True, "events_verified": int(total or 0)}
    return {
        "chain_intact": False,
        "broken_at": int(rows[0]["broken_at"]),
        "reason": rows[0]["reason"],
    }


async def export(
    conn: asyncpg.Connection,
    *,
    since_id: int = 0,
    limit: int = 500,
    entity_type: str | None = None,
) -> list[dict[str, Any]]:
    """Tenant-scoped audit export. RLS restricts rows to the caller's tenant."""
    rows = await conn.fetch(
        """
        SELECT id, tenant_id, actor_id, actor_role, action, entity_type,
               entity_id, detail, prev_hash, row_hash, occurred_at
          FROM audit_event
         WHERE id > $1
           AND ($2::text IS NULL OR entity_type = $2)
         ORDER BY id
         LIMIT $3
        """,
        since_id,
        entity_type,
        limit,
    )
    return [dict(r) for r in rows]
