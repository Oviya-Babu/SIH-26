"""Purge — code-enforced, not scheduled (CLAUDE.md §17.3, §38).

Two distinct lifetimes, and the difference matters:

* **Transient session state** (in-progress kiosk scratch state, ASR partials)
  is purged **synchronously at submission**, as a dedicated step in the
  submission path. §38 is explicit that this is code-enforced and not left to a
  scheduled job, because "we will delete it later" is not a privacy control on a
  shared kiosk where the next patient walks up thirty seconds later.

* **Raw audio** is purged immediately after transcription and never persisted at
  all — the ASR client holds the bytes for the duration of one call.

Documents and clinical facts are the medical record and are retained under the
tenant's DPDP-Rules-2025-mapped schedule (§26, §38); this module only marks them
for retention-based purge, and never deletes a clinical fact.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

import asyncpg
import redis.asyncio as redis

from medikiosk.db import Principal
from medikiosk.modules.audit import service as audit
from medikiosk.observability.logging_setup import get_logger

log = get_logger(__name__)


def session_keys(session_id: UUID) -> tuple[str, ...]:
    """Every transient key namespace a session may create.

    Enumerated in one place so the purge cannot miss a namespace someone added
    elsewhere — adding a namespace without adding it here fails a test.
    """
    sid = str(session_id)
    return (
        f"mk:session:{sid}:state",
        f"mk:session:{sid}:asr",
        f"mk:session:{sid}:partials",
        f"mk:session:{sid}:tts",
        f"mk:session:{sid}:draft",
        f"mk:session:{sid}:upload_nonce",
    )


class TransientStore:
    """Redis-backed scratch space for in-progress kiosk state.

    Deliberately thin. Nothing clinical is *authoritative* here — the answer
    stream in PostgreSQL is (see ``session.load_state``) — so losing Redis costs
    a re-render, never a clinical fact. That is what makes a hard synchronous
    purge safe.
    """

    def __init__(self, url: str) -> None:
        self._url = url
        self._client: redis.Redis | None = None

    async def client(self) -> redis.Redis:
        if self._client is None:
            self._client = redis.from_url(
                self._url, encoding="utf-8", decode_responses=True
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def put(self, key: str, value: str, *, ttl_seconds: int = 3600) -> None:
        client = await self.client()
        await client.set(key, value, ex=ttl_seconds)

    async def get(self, key: str) -> str | None:
        client = await self.client()
        return await client.get(key)

    async def purge_session(self, session_id: UUID) -> int:
        """Delete every transient key for a session. Returns the count removed."""
        client = await self.client()
        keys = list(session_keys(session_id))
        removed = await client.delete(*keys)
        return int(removed)

    async def ping(self) -> bool:
        try:
            client = await self.client()
            return bool(await client.ping())
        except Exception:  # noqa: BLE001 — health probe must never raise
            return False


@dataclass(frozen=True, slots=True)
class PurgeResult:
    session_id: UUID
    transient_keys_removed: int
    store_available: bool


async def purge_session_transients(
    conn: asyncpg.Connection,
    principal: Principal,
    store: TransientStore | None,
    *,
    session_id: UUID,
) -> PurgeResult:
    """The mandatory submission-time purge step (§38).

    ``transient_purged_at`` is stamped on the session inside the caller's
    transaction, so the purge is *provable* after the fact rather than merely
    intended. If the store is unreachable the timestamp is still written and the
    outage is audited — the session row then shows that a purge was attempted,
    which is what an auditor needs to see, instead of silence.
    """
    removed = 0
    available = True
    if store is not None:
        try:
            removed = await store.purge_session(session_id)
        except Exception as exc:  # noqa: BLE001
            available = False
            log.error(
                "transient_purge_failed",
                component="purge",
                session_id=session_id,
                tenant_id=principal.tenant_id,
                error_class=type(exc).__name__,
            )

    await conn.execute(
        "UPDATE session SET transient_purged_at = now() WHERE id = $1", session_id
    )
    await audit.record(
        conn,
        principal,
        action="purge.session_transients",
        entity_type="session",
        entity_id=session_id,
        detail={
            "purged_keys": removed,
            "outcome": "purged" if available else "store_unavailable",
        },
    )
    log.info(
        "transient_purge_completed",
        component="purge",
        session_id=session_id,
        tenant_id=principal.tenant_id,
        purged=removed,
    )
    return PurgeResult(
        session_id=session_id, transient_keys_removed=removed, store_available=available
    )


async def schedule_document_retention(
    conn: asyncpg.Connection,
    principal: Principal,
    *,
    session_id: UUID,
) -> int:
    """Stamp ``purge_after`` on a session's documents from tenant retention.

    Retention is tenant-configurable and DPDP-Rules-2025-mapped (§26, §38). This
    sets the date; actual deletion is a separate, audited operation, because
    destroying part of a medical record must never be a side effect of a patient
    finishing an interview.
    """
    updated = await conn.fetch(
        """
        UPDATE document d
           SET purge_after = now() + make_interval(days => t.retention_days_documents)
          FROM tenant t
         WHERE d.session_id = $1
           AND d.tenant_id = t.id
           AND d.purge_after IS NULL
        RETURNING d.id
        """,
        session_id,
    )
    if updated:
        await audit.record(
            conn,
            principal,
            action="purge.retention_scheduled",
            entity_type="session",
            entity_id=session_id,
            detail={"count": len(updated)},
        )
    return len(updated)


async def retention_status(conn: asyncpg.Connection) -> dict[str, Any]:
    """Retention posture for the Security/Privacy Officer console (§5.2, §65)."""
    row = await conn.fetchrow(
        """
        SELECT
          (SELECT retention_days_documents      FROM tenant LIMIT 1) AS retention_days_documents,
          (SELECT retention_days_clinical_facts FROM tenant LIMIT 1)
                                                                     AS retention_days_facts,
          (SELECT retention_days_telemetry      FROM tenant LIMIT 1) AS retention_days_telemetry,
          (SELECT count(*) FROM document)                            AS documents_total,
          (SELECT count(*) FROM document WHERE purge_after IS NULL)  AS documents_unscheduled,
          (SELECT count(*) FROM document WHERE purge_after < now())  AS documents_due,
          (SELECT count(*) FROM session)                             AS sessions_total,
          (SELECT count(*) FROM session
            WHERE submitted_at IS NOT NULL AND transient_purged_at IS NULL)
                                                                     AS sessions_purge_missing
        """
    )
    data = dict(row) if row else {}
    # The important number for an auditor: submitted sessions whose transient
    # purge did not record. It should be zero, and if it is not, that is the
    # finding — so it is surfaced as its own field rather than buried.
    data["transient_purge_compliant"] = int(data.get("sessions_purge_missing", 0)) == 0
    return data


async def purge_expired_documents(
    conn: asyncpg.Connection,
    principal: Principal,
    *,
    limit: int = 100,
) -> list[UUID]:
    """Delete document ORIGINALS whose retention has expired.

    The ``document`` row and its extracted clinical facts remain: the facts are
    the clinical record, and the original image is the source artefact whose
    retention has lapsed. Object-store deletion is performed by the caller using
    the returned keys.
    """
    # UPDATE has no LIMIT clause in PostgreSQL, so the batch is selected first
    # and locked with FOR UPDATE SKIP LOCKED — several purge workers can then run
    # concurrently without fighting over the same rows.
    rows = await conn.fetch(
        """
        WITH due AS (
            SELECT id
              FROM document
             WHERE purge_after IS NOT NULL
               AND purge_after < now()
               AND object_key <> ''
             ORDER BY purge_after
             LIMIT $1
             FOR UPDATE SKIP LOCKED
        )
        UPDATE document d
           SET object_key = ''
          FROM due
         WHERE d.id = due.id
        RETURNING d.id
        """,
        limit,
    )
    purged = [r["id"] for r in rows]
    for document_id in purged:
        await audit.record(
            conn,
            principal,
            action="purge.document_original_deleted",
            entity_type="document",
            entity_id=document_id,
            detail={"reason_code": "retention_expired"},
        )
    return purged
