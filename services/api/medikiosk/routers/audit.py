"""Audit export and chain verification (CLAUDE.md §31, §49).

``GET /v1/audit/export`` carries step-up auth (§49): it is the single endpoint
that can emit a tenant's whole action history, so it requires the
``security_officer`` role AND an MFA-satisfied token, and it audits its own
invocation.

Detail payloads are already allowlist-filtered at write time (§31), so an export
cannot leak clinical content that was never stored in the first place.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query

from medikiosk.deps import Ctx, StaffPrincipal, require
from medikiosk.modules.audit import service as audit_service
from medikiosk.security.opa import ResourceContext
from medikiosk.security.rbac import Capability

router = APIRouter(prefix="/v1/audit", tags=["audit"])


@router.get("/export")
async def export_audit(
    ctx: Ctx,
    principal: StaffPrincipal,
    authz: Annotated[Any, Depends(require(Capability.AUDIT_EXPORT, "export"))],
    since_id: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
    entity_type: str | None = None,
) -> dict[str, Any]:
    """Export audit rows, cursor-paginated by id.

    ``since_id`` paginates on the monotonic primary key rather than a timestamp,
    so an exporter can prove it saw every row with no gap — which is the property
    that makes a hash chain worth having.
    """
    await authz.check(
        ResourceContext(type="audit_event", tenant_id=principal.tenant_id)
    )

    async with ctx.db.transaction(principal) as conn:
        rows = await audit_service.export(
            conn, since_id=since_id, limit=limit, entity_type=entity_type
        )
        # The export is itself an auditable act, recorded in the same chain.
        await audit_service.record(
            conn,
            principal,
            action="audit.exported",
            entity_type="audit_event",
            entity_id=principal.tenant_id,
            detail={
                "count": len(rows),
                "step_up_verified": True,
                "entity_type": entity_type or "all",
            },
        )

    return {
        "count": len(rows),
        "next_since_id": rows[-1]["id"] if rows else since_id,
        "has_more": len(rows) == limit,
        "events": [
            {
                "id": r["id"],
                "actor_role": r["actor_role"],
                "actor_ref": str(r["actor_id"]) if r["actor_id"] else None,
                "action": r["action"],
                "entity_type": r["entity_type"],
                "entity_id": str(r["entity_id"]),
                "detail": r["detail"],
                "prev_hash": r["prev_hash"],
                "row_hash": r["row_hash"],
                "occurred_at": r["occurred_at"],
            }
            for r in rows
        ],
    }


@router.get("/verify")
async def verify_chain(
    ctx: Ctx,
    principal: StaffPrincipal,
    authz: Annotated[Any, Depends(require(Capability.AUDIT_VERIFY, "verify"))],
) -> dict[str, Any]:
    """Recompute the hash chain and report the first break, if any (§31).

    Verification runs in the database, over every row, in order. A broken chain
    is reported with the exact id where it breaks — an auditor needs the location,
    not just a boolean.
    """
    await authz.check(
        ResourceContext(type="audit_chain", tenant_id=principal.tenant_id)
    )
    async with ctx.db.transaction(principal) as conn:
        result = await audit_service.verify_chain(conn)
        await audit_service.record(
            conn,
            principal,
            action="audit.chain_verified",
            entity_type="audit_event",
            entity_id=principal.tenant_id,
            detail={
                "chain_intact": result["chain_intact"],
                "broken_at": result.get("broken_at"),
            },
        )
    return {
        **result,
        "note": (
            "audit_event is append-only at the DB grant level: no application role, "
            "including the API's, holds UPDATE or DELETE on it (CLAUDE.md §31)."
        ),
    }


@router.get("/entity/{entity_type}/{entity_id}")
async def entity_trail(
    ctx: Ctx,
    entity_type: str,
    entity_id: str,
    principal: StaffPrincipal,
    authz: Annotated[Any, Depends(require(Capability.AUDIT_VERIFY, "read"))],
) -> dict[str, Any]:
    """The audit trail for one entity — 'who touched this record'."""
    await authz.check(
        ResourceContext(type="audit_event", tenant_id=principal.tenant_id)
    )
    async with ctx.db.readonly(principal) as conn:
        rows = await conn.fetch(
            """
            SELECT id, actor_id, actor_role, action, detail, row_hash, occurred_at
              FROM audit_event
             WHERE entity_type = $1 AND entity_id = $2::uuid
             ORDER BY id
            """,
            entity_type,
            entity_id,
        )
    return {
        "entity_type": entity_type,
        "entity_id": entity_id,
        "events": [dict(r) for r in rows],
    }
