#!/usr/bin/env python3
"""Synthetic demo data (CLAUDE.md §28, §55, §64).

[RED LINE §28] Dev/staging are SYNTHETIC-ONLY. Every name, identifier and
clinical detail created here is invented. There is no production-to-staging PHI
pipeline in this system and this script is not one.

It provisions what the §64 demonstration needs:

* two tenants — the second exists solely so cross-tenant isolation can be PROVEN
  rather than asserted (§64.8);
* General Medicine and AYUSH departments;
* a kiosk device per department, with credentials printed once;
* the seven roles of §5.2 as local user projections;
* nothing clinical. Clinical data is produced by running the actual workflow,
  because a seeded "clinical record" would not exercise the engine.

    python scripts/seed_demo.py --dsn postgresql://owner@host/medikiosk
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import secrets
import sys
from pathlib import Path
from typing import Any

import asyncpg

REPO_ROOT = Path(__file__).resolve().parents[1]
CONTENT_ROOT = REPO_ROOT / "content"

TENANTS: tuple[dict[str, Any], ...] = (
    {
        "slug": "sih-demo-hospital",
        "display_name": "SIH Demonstration District Hospital",
        "primary": True,
    },
    {
        # Exists ONLY to prove tenant isolation. A physician in tenant A must get
        # a real 403 for a session in tenant B (§64.8).
        "slug": "isolation-control-hospital",
        "display_name": "Isolation Control Hospital (test tenant)",
        "primary": False,
    },
)

DEPARTMENTS: tuple[dict[str, str], ...] = (
    {"code": "GEN-MED", "display_name": "General Medicine OPD",
     "protocol_family": "general_medicine"},
    {"code": "AYUSH-AYU", "display_name": "Ayurveda OPD (AYUSH)",
     "protocol_family": "ayush_ayurveda"},
)

# The seven roles of §5.2. Subjects match the Keycloak realm import so the OIDC
# chain lines up without hand-editing either side.
USERS: tuple[dict[str, Any], ...] = (
    {"subject": "kc-nurse-genmed", "username": "nurse.genmed",
     "display_name": "Nurse Anitha Raman", "role": "nurse", "department": "GEN-MED",
     "mfa": False},
    {"subject": "kc-nurse-ayush", "username": "nurse.ayush",
     "display_name": "Nurse Sujatha Nair", "role": "nurse", "department": "AYUSH-AYU",
     "mfa": False},
    {"subject": "kc-physician-genmed", "username": "physician.genmed",
     "display_name": "Dr Vikram Iyer", "role": "physician", "department": "GEN-MED",
     "mfa": True},
    {"subject": "kc-physician-genmed-2", "username": "physician.genmed2",
     "display_name": "Dr Fatima Sheikh", "role": "physician", "department": "GEN-MED",
     "mfa": True},
    {"subject": "kc-ayush-practitioner", "username": "practitioner.ayush",
     "display_name": "Dr Meenakshi Pillai (BAMS)", "role": "ayush_practitioner",
     "department": "AYUSH-AYU", "mfa": True},
    {"subject": "kc-clinical-admin", "username": "governance.admin",
     "display_name": "Dr R Krishnan (Clinical Governance)", "role": "clinical_admin",
     "department": None, "mfa": True},
    {"subject": "kc-it-admin", "username": "it.admin",
     "display_name": "S Prakash (IT Administrator)", "role": "it_admin",
     "department": None, "mfa": True},
    {"subject": "kc-security-officer", "username": "security.officer",
     "display_name": "L Menon (Security & Privacy Officer)", "role": "security_officer",
     "department": None, "mfa": True},
)

DEVICES: tuple[dict[str, str], ...] = (
    {"label": "KIOSK-GENMED-01", "department": "GEN-MED", "device_type": "kiosk_tablet"},
    {"label": "KIOSK-AYUSH-01", "department": "AYUSH-AYU", "device_type": "kiosk_tablet"},
    {"label": "STAFF-CAPTURE-01", "department": "GEN-MED", "device_type": "staff_capture"},
)


def content_checksum(family: str, version: str) -> str | None:
    """Compute the checksum governance should pin for a protocol version.

    For a composed protocol (AYUSH extends General Medicine) the checksum covers
    base + deriving content, matching what the registry computes at load.
    """
    path = CONTENT_ROOT / "protocols" / family / f"{version}.json"
    if not path.is_file():
        return None
    raw = path.read_bytes()
    document = json.loads(raw)
    extends = document.get("extends")
    if not extends:
        return hashlib.sha256(raw).hexdigest()
    base = CONTENT_ROOT / "protocols" / extends["family"] / f"{extends['version']}.json"
    if not base.is_file():
        return None
    return hashlib.sha256(base.read_bytes() + b"\x00" + raw).hexdigest()


async def seed(dsn: str, *, pin_checksums: bool) -> dict[str, Any]:
    conn = await asyncpg.connect(dsn)
    report: dict[str, Any] = {"tenants": [], "devices": [], "users": 0}
    try:
        async with conn.transaction():
            for tenant_spec in TENANTS:
                tenant_id = await conn.fetchval(
                    """
                    INSERT INTO tenant (slug, display_name)
                    VALUES ($1, $2)
                    ON CONFLICT (slug) DO UPDATE SET display_name = EXCLUDED.display_name
                    RETURNING id
                    """,
                    tenant_spec["slug"],
                    tenant_spec["display_name"],
                )
                tenant_report: dict[str, Any] = {
                    "slug": tenant_spec["slug"],
                    "tenant_id": str(tenant_id),
                    "departments": {},
                }

                department_ids: dict[str, Any] = {}
                for dept in DEPARTMENTS:
                    dept_id = await conn.fetchval(
                        """
                        INSERT INTO department (tenant_id, code, display_name, protocol_family)
                        VALUES ($1, $2, $3, $4)
                        ON CONFLICT (tenant_id, code)
                            DO UPDATE SET display_name = EXCLUDED.display_name
                        RETURNING id
                        """,
                        tenant_id,
                        dept["code"],
                        dept["display_name"],
                        dept["protocol_family"],
                    )
                    department_ids[dept["code"]] = dept_id
                    tenant_report["departments"][dept["code"]] = str(dept_id)

                    await conn.execute(
                        """
                        INSERT INTO tenant_protocol_config
                            (tenant_id, protocol_family, active_version)
                        VALUES ($1, $2, 'v1')
                        ON CONFLICT (tenant_id, protocol_family)
                            DO UPDATE SET active_version = 'v1', updated_at = now()
                        """,
                        tenant_id,
                        dept["protocol_family"],
                    )

                if tenant_spec["primary"]:
                    for user in USERS:
                        await conn.execute(
                            """
                            INSERT INTO app_user (tenant_id, subject, username, display_name,
                                                  role, assigned_department_id, mfa_enrolled)
                            VALUES ($1, $2, $3, $4, $5, $6, $7)
                            ON CONFLICT (subject) DO UPDATE
                               SET display_name = EXCLUDED.display_name,
                                   role = EXCLUDED.role,
                                   assigned_department_id = EXCLUDED.assigned_department_id,
                                   mfa_enrolled = EXCLUDED.mfa_enrolled,
                                   updated_at = now()
                            """,
                            tenant_id,
                            user["subject"],
                            user["username"],
                            user["display_name"],
                            user["role"],
                            department_ids.get(user["department"]) if user["department"] else None,
                            user["mfa"],
                        )
                    report["users"] = len(USERS)

                    for device in DEVICES:
                        existing = await conn.fetchval(
                            "SELECT id FROM device WHERE tenant_id = $1 AND label = $2",
                            tenant_id,
                            device["label"],
                        )
                        if existing is not None:
                            report["devices"].append(
                                {"label": device["label"], "credential": "(already provisioned)"}
                            )
                            continue
                        credential = secrets.token_urlsafe(48)
                        await conn.execute(
                            """
                            INSERT INTO device (tenant_id, department_id, label,
                                                credential_hash, device_type)
                            VALUES ($1, $2, $3, $4, $5)
                            """,
                            tenant_id,
                            department_ids[device["department"]],
                            device["label"],
                            hashlib.sha256(credential.encode()).hexdigest(),
                            device["device_type"],
                        )
                        report["devices"].append(
                            {"label": device["label"], "credential": credential}
                        )
                else:
                    # The control tenant gets one physician, so a cross-tenant
                    # access attempt has a real identity to attempt it with.
                    await conn.execute(
                        """
                        INSERT INTO app_user (tenant_id, subject, username, display_name,
                                              role, assigned_department_id, mfa_enrolled)
                        VALUES ($1, 'kc-physician-othertenant', 'physician.other',
                                'Dr Other Tenant', 'physician', $2, true)
                        ON CONFLICT (subject) DO UPDATE
                           SET assigned_department_id = EXCLUDED.assigned_department_id
                        """,
                        tenant_id,
                        department_ids["GEN-MED"],
                    )

                report["tenants"].append(tenant_report)

            if pin_checksums:
                for family in ("general_medicine", "ayush_ayurveda"):
                    checksum = content_checksum(family, "v1")
                    if checksum:
                        await conn.execute(
                            """
                            UPDATE protocol_version
                               SET content_checksum = $3
                             WHERE protocol_family = $1 AND version = $2
                            """,
                            family,
                            "v1",
                            checksum,
                        )
                report["checksums_pinned"] = True
    finally:
        await conn.close()
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed synthetic demo data")
    parser.add_argument(
        "--dsn",
        default=os.environ.get("MEDIKIOSK_MIGRATION_DSN")
        or os.environ.get("MEDIKIOSK_OWNER_DATABASE_URL"),
    )
    parser.add_argument(
        "--pin-checksums",
        action="store_true",
        help="pin the current protocol content checksums as governance-approved",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args()

    if not args.dsn:
        print("no DSN provided; pass --dsn or set MEDIKIOSK_MIGRATION_DSN", file=sys.stderr)
        return 2

    report = asyncio.run(seed(args.dsn, pin_checksums=args.pin_checksums))

    if args.json:
        print(json.dumps(report, indent=2))
        return 0

    print("SYNTHETIC DEMO DATA — every name and identifier here is invented (§28).\n")
    for tenant in report["tenants"]:
        print(f"tenant {tenant['slug']}  id={tenant['tenant_id']}")
        for code, dept_id in tenant["departments"].items():
            print(f"    department {code:12} id={dept_id}")
    print(f"\nstaff users provisioned: {report['users']}")
    print("\ndevice credentials (shown once; stored only as a digest):")
    for device in report["devices"]:
        print(f"    {device['label']:20} {device['credential']}")
    if report.get("checksums_pinned"):
        print("\nprotocol content checksums pinned as governance-approved")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
