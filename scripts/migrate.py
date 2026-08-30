#!/usr/bin/env python3
"""Migration runner (CLAUDE.md §48).

Migrations are plain SQL, sequenced by a single owner to prevent numbering
collisions across parallel workstreams (§48). Each file is applied once, inside a
transaction, and its checksum is recorded — so an edit to an already-applied
migration is detected and refused rather than silently ignored.

Run as the database OWNER (not the API's ``medikiosk_app`` role): migrations
create roles, grants and SECURITY DEFINER functions, and the API role is
deliberately not permitted to do any of that.

    python scripts/migrate.py --dsn postgresql://owner@host/medikiosk
    python scripts/migrate.py --status
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import re
import sys
from pathlib import Path

import asyncpg

REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS_DIR = REPO_ROOT / "migrations"
_VERSION_RE = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")

BOOTSTRAP = """
CREATE TABLE IF NOT EXISTS schema_migration (
    version     TEXT PRIMARY KEY,
    checksum    TEXT NOT NULL,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


def discover() -> list[tuple[str, Path, str]]:
    """Return (version, path, checksum) in sequence order."""
    if not MIGRATIONS_DIR.is_dir():
        raise SystemExit(f"migrations directory not found: {MIGRATIONS_DIR}")

    found: list[tuple[str, Path, str]] = []
    seen_numbers: dict[str, str] = {}
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        match = _VERSION_RE.match(path.name)
        if match is None:
            raise SystemExit(
                f"migration {path.name!r} does not match NNNN_snake_case.sql — "
                "migrations are sequenced by a single owner (CLAUDE.md §48)"
            )
        number = match.group(1)
        if number in seen_numbers:
            raise SystemExit(
                f"duplicate migration number {number}: "
                f"{seen_numbers[number]} and {path.name}"
            )
        seen_numbers[number] = path.name
        raw = path.read_bytes()
        found.append((path.stem, path, hashlib.sha256(raw).hexdigest()))
    return found


async def apply(dsn: str, *, dry_run: bool = False) -> int:
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(BOOTSTRAP)
        applied = {
            r["version"]: r["checksum"]
            for r in await conn.fetch("SELECT version, checksum FROM schema_migration")
        }

        pending = []
        for version, path, checksum in discover():
            if version in applied:
                if applied[version] != checksum:
                    raise SystemExit(
                        f"migration {version} was modified after being applied.\n"
                        "Applied migrations are immutable: add a new migration instead."
                    )
                continue
            pending.append((version, path, checksum))

        if not pending:
            print("nothing to apply; schema is current")
            return 0

        for version, path, checksum in pending:
            print(f"applying {version} …", end=" ", flush=True)
            if dry_run:
                print("(dry run)")
                continue
            sql = path.read_text("utf-8")
            async with conn.transaction():
                await conn.execute(sql)
                await conn.execute(
                    "INSERT INTO schema_migration (version, checksum) VALUES ($1, $2)",
                    version,
                    checksum,
                )
            print("ok")

        return len(pending)
    finally:
        await conn.close()


async def status(dsn: str) -> None:
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(BOOTSTRAP)
        applied = {
            r["version"]: r for r in await conn.fetch("SELECT * FROM schema_migration")
        }
        print(f"{'version':32} {'state':10} applied_at")
        for version, _, checksum in discover():
            row = applied.get(version)
            if row is None:
                print(f"{version:32} {'pending':10} -")
            elif row["checksum"] != checksum:
                print(f"{version:32} {'MODIFIED':10} {row['applied_at']}")
            else:
                print(f"{version:32} {'applied':10} {row['applied_at']}")

        # RLS posture: the point of §30 is that this is verifiable, not assumed.
        rows = await conn.fetch(
            """
            SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity,
                   (SELECT count(*) FROM pg_policies p WHERE p.tablename = c.relname) AS policies
              FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE n.nspname = 'public' AND c.relkind = 'r'
             ORDER BY c.relname
            """
        )
        if rows:
            print("\nRLS posture (CLAUDE.md §30):")
            unprotected = []
            for r in rows:
                mark = "ok " if (r["relrowsecurity"] and r["relforcerowsecurity"]) else "OFF"
                if mark == "OFF" and r["relname"] not in (
                    "schema_migration", "protocol_version", "lab_reference_range"
                ):
                    unprotected.append(r["relname"])
                print(f"  {mark} {r['relname']:32} policies={r['policies']}")
            if unprotected:
                print(f"\n  WARNING: RLS not forced on: {', '.join(unprotected)}")
    finally:
        await conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="MediKiosk migration runner")
    parser.add_argument(
        "--dsn",
        default=os.environ.get("MEDIKIOSK_MIGRATION_DSN")
        or os.environ.get("MEDIKIOSK_OWNER_DATABASE_URL"),
        help="owner DSN (env: MEDIKIOSK_MIGRATION_DSN)",
    )
    parser.add_argument("--status", action="store_true", help="show state and exit")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not args.dsn:
        print(
            "no DSN provided. Pass --dsn or set MEDIKIOSK_MIGRATION_DSN.\n"
            "Migrations must run as the database OWNER, not as medikiosk_app.",
            file=sys.stderr,
        )
        return 2

    if args.status:
        asyncio.run(status(args.dsn))
        return 0

    applied = asyncio.run(apply(args.dsn, dry_run=args.dry_run))
    print(f"{applied} migration(s) applied")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
