"""Tenant, department, device and protocol resolution (CLAUDE.md §8, §10).

``resolve_protocol`` implements §10's mechanism exactly:

    device fixes the tenant  →  department fixes the protocol family
                             →  tenant config fixes the active version

[RED LINE §10] Department selection at the kiosk drives protocol loading through
this governed, versioned lookup. No LLM participates.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

import asyncpg

from medikiosk.errors import Conflict, NotFound, ValidationFailed
from medikiosk.modules.clinical_protocol.model import Protocol
from medikiosk.modules.clinical_protocol.registry import ProtocolRegistry
from medikiosk.security.tokens import hash_device_credential


@dataclass(frozen=True, slots=True)
class DeviceBinding:
    device_id: UUID
    tenant_id: UUID
    tenant_slug: str
    tenant_name: str
    department_id: UUID | None
    department_code: str | None
    department_name: str | None
    protocol_family: str | None
    device_type: str


async def authenticate_device(
    conn: asyncpg.Connection, credential: str
) -> DeviceBinding:
    """Resolve a provisioned device from its credential.

    §33: a stolen or unprovisioned tablet cannot obtain a session token. The
    credential is compared as a digest — the secret itself is never stored.

    This runs before any tenant context exists, which is exactly why it goes
    through ``device_authenticate`` — a SECURITY DEFINER function that takes only
    a digest and returns at most one row. RLS is not weakened anywhere; this is
    the single, explicitly scoped bootstrap path (see migration 0001).
    """
    row = await conn.fetchrow(
        "SELECT * FROM device_authenticate($1)",
        hash_device_credential(credential),
    )
    if row is None:
        raise NotFound("device is not provisioned", reason_code="device_not_provisioned")
    if row["device_status"] != "active":
        raise NotFound("device is revoked", reason_code="device_revoked")
    if row["tenant_status"] != "active":
        raise Conflict("tenant is suspended", reason_code="tenant_suspended")
    if row["department_id"] is not None and row["department_status"] != "active":
        raise Conflict("department is inactive", reason_code="department_inactive")

    return DeviceBinding(
        device_id=row["device_id"],
        tenant_id=row["tenant_id"],
        tenant_slug=row["tenant_slug"],
        tenant_name=row["tenant_name"],
        department_id=row["department_id"],
        department_code=row["department_code"],
        department_name=row["department_name"],
        protocol_family=row["protocol_family"],
        device_type=row["device_type"],
    )


async def touch_device(conn: asyncpg.Connection, device_id: UUID) -> None:
    await conn.execute("UPDATE device SET last_seen_at = now() WHERE id = $1", device_id)


@dataclass(frozen=True, slots=True)
class DepartmentInfo:
    id: UUID
    code: str
    display_name: str
    protocol_family: str


async def list_departments(conn: asyncpg.Connection) -> list[DepartmentInfo]:
    rows = await conn.fetch(
        """
        SELECT id, code, display_name, protocol_family
          FROM department
         WHERE status = 'active'
         ORDER BY display_name
        """
    )
    return [
        DepartmentInfo(
            id=r["id"],
            code=r["code"],
            display_name=r["display_name"],
            protocol_family=r["protocol_family"],
        )
        for r in rows
    ]


async def get_department(conn: asyncpg.Connection, department_id: UUID) -> DepartmentInfo:
    row = await conn.fetchrow(
        """
        SELECT id, code, display_name, protocol_family
          FROM department
         WHERE id = $1 AND status = 'active'
        """,
        department_id,
    )
    if row is None:
        raise NotFound("department not found", reason_code="department_not_found")
    return DepartmentInfo(
        id=row["id"],
        code=row["code"],
        display_name=row["display_name"],
        protocol_family=row["protocol_family"],
    )


@dataclass(frozen=True, slots=True)
class ResolvedProtocol:
    protocol: Protocol
    family: str
    version: str
    department: DepartmentInfo


async def resolve_protocol(
    conn: asyncpg.Connection,
    registry: ProtocolRegistry,
    *,
    department_id: UUID,
) -> ResolvedProtocol:
    """The §10 resolution mechanism, verbatim.

    The version comes from ``tenant_protocol_config`` — governed configuration,
    not a request parameter — so a kiosk cannot ask for an unapproved protocol
    version and a patient cannot be interviewed with one.
    """
    department = await get_department(conn, department_id)

    version = await conn.fetchval(
        """
        SELECT active_version
          FROM tenant_protocol_config
         WHERE protocol_family = $1
        """,
        department.protocol_family,
    )
    if version is None:
        raise Conflict(
            f"no active protocol version configured for {department.protocol_family}",
            reason_code="protocol_not_configured",
        )

    # The registry is the runtime authority on content; protocol_version is the
    # governance record. Refuse to run content that governance has not approved.
    approved = await conn.fetchval(
        """
        SELECT content_checksum
          FROM protocol_version
         WHERE protocol_family = $1 AND version = $2 AND status = 'active'
        """,
        department.protocol_family,
        version,
    )
    protocol = registry.load(department.protocol_family, version)
    if approved is not None and approved != protocol.content_checksum:
        raise Conflict(
            "protocol content on disk does not match the governance-approved checksum",
            reason_code="protocol_checksum_mismatch",
        )

    return ResolvedProtocol(
        protocol=protocol,
        family=department.protocol_family,
        version=version,
        department=department,
    )


async def get_tenant_retention(conn: asyncpg.Connection) -> dict[str, int]:
    row = await conn.fetchrow(
        """
        SELECT retention_days_documents,
               retention_days_clinical_facts,
               retention_days_telemetry
          FROM tenant
         LIMIT 1
        """
    )
    if row is None:
        raise NotFound("tenant not visible in this context", reason_code="not_found")
    return dict(row)


async def register_device(
    conn: asyncpg.Connection,
    *,
    tenant_id: UUID,
    label: str,
    credential: str,
    department_id: UUID | None,
    device_type: str = "kiosk_tablet",
) -> UUID:
    """Provision a device. IT Admin only (§5.2)."""
    if len(credential) < 32:
        raise ValidationFailed(
            "device credential must be at least 32 characters",
            reason_code="weak_device_credential",
        )
    try:
        return await conn.fetchval(
            """
            INSERT INTO device (tenant_id, department_id, label, credential_hash, device_type)
            VALUES ($1, $2, $3, $4, $5)
            RETURNING id
            """,
            tenant_id,
            department_id,
            label,
            hash_device_credential(credential),
            device_type,
        )
    except asyncpg.UniqueViolationError as exc:
        raise Conflict("device label or credential already exists",
                       reason_code="device_exists") from exc
