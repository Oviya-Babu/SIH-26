"""Database access with mandatory RLS context (CLAUDE.md §30).

The single rule this module exists to enforce: **you cannot obtain a database
connection without declaring who you are.** ``Database.transaction()`` requires a
:class:`Principal` and sets the session GUCs that every RLS policy reads, using
``set_config(..., is_local => true)`` so the context dies with the transaction
and can never leak to the next borrower of a pooled connection.

The API connects as ``medikiosk_app``, which is ``NOBYPASSRLS``. That is the
backstop of §30: even if application code forgets a ``WHERE tenant_id = ...``,
the database still refuses to return another tenant's rows.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import asyncpg

from medikiosk.config import Settings
from medikiosk.observability.logging_setup import get_logger

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class Principal:
    """The authenticated identity a transaction runs as.

    ``role`` is the MediKiosk role name, not a database role. ``patient_id`` is
    set only for the kiosk/patient principal and drives the patient-self RLS
    policies.
    """

    tenant_id: UUID
    role: str
    actor_id: UUID | None = None
    patient_id: UUID | None = None
    session_id: UUID | None = None
    department_id: UUID | None = None
    mfa_satisfied: bool = False
    subject: str | None = None
    # For a caregiver respondent: the sessions they are authorised to act in (§6)
    authorized_session_ids: tuple[UUID, ...] = ()

    @property
    def is_patient_tier(self) -> bool:
        return self.role in ("patient", "caregiver_respondent")


# A principal used only by internal maintenance paths that legitimately operate
# across a single tenant without a human actor (purge, retention, relay export).
def system_principal(tenant_id: UUID, *, role: str = "system") -> Principal:
    return Principal(tenant_id=tenant_id, role=role, actor_id=None)


class Database:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        if self._pool is not None:
            return
        self._pool = await asyncpg.create_pool(
            dsn=self._settings.database_url,
            min_size=self._settings.database_pool_min,
            max_size=self._settings.database_pool_max,
            command_timeout=self._settings.database_statement_timeout_ms / 1000,
            init=_init_connection,
            server_settings={
                "application_name": self._settings.service_name,
                "statement_timeout": str(self._settings.database_statement_timeout_ms),
            },
        )
        await self._assert_rls_posture()
        log.info("db_pool_ready", component="db")

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    @property
    def pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError("database pool is not initialised")
        return self._pool

    async def _assert_rls_posture(self) -> None:
        """Fail startup if the connected role could bypass RLS.

        §30 calls RLS the backstop that holds even when application code has a
        bug. A superuser or BYPASSRLS connection silently removes that backstop,
        so we refuse to start rather than run without it.
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT rolsuper, rolbypassrls, current_user AS name "
                "FROM pg_roles WHERE rolname = current_user"
            )
            if row is None:
                raise RuntimeError("cannot determine connected database role")
            if row["rolsuper"] or row["rolbypassrls"]:
                raise RuntimeError(
                    f"API is connected as {row['name']!r}, which bypasses Row Level "
                    "Security. Connect as medikiosk_app (CLAUDE.md §30 [RED LINE])."
                )
            # Every patient-data table must actually have RLS forced on.
            unprotected = await conn.fetch(
                """
                SELECT c.relname
                  FROM pg_class c
                  JOIN pg_namespace n ON n.oid = c.relnamespace
                 WHERE n.nspname = 'public'
                   AND c.relkind = 'r'
                   AND c.relname = ANY($1::text[])
                   AND NOT (c.relrowsecurity AND c.relforcerowsecurity)
                """,
                list(PATIENT_DATA_TABLES),
            )
            if unprotected:
                names = ", ".join(r["relname"] for r in unprotected)
                raise RuntimeError(
                    f"RLS is not enabled+forced on: {names} (CLAUDE.md §30 [RED LINE])"
                )

    @asynccontextmanager
    async def transaction(self, principal: Principal) -> AsyncIterator[asyncpg.Connection]:
        """Open a transaction bound to ``principal``'s RLS context."""
        async with self.pool.acquire() as conn, conn.transaction():
            await _apply_principal(conn, principal)
            yield conn

    @asynccontextmanager
    async def readonly(self, principal: Principal) -> AsyncIterator[asyncpg.Connection]:
        async with self.pool.acquire() as conn, conn.transaction(readonly=True):
            await _apply_principal(conn, principal)
            yield conn


PATIENT_DATA_TABLES: Sequence[str] = (
    "tenant",
    "department",
    "tenant_protocol_config",
    "app_user",
    "device",
    "patient",
    "audit_event",
    "consent",
    "abdm_consent_artifact_ref",
    "caregiver_authorization",
    "session",
    "session_answer",
    "upload_token",
    "clinical_fact",
    "red_flag_evaluation",
    "red_flag_alert",
    "document",
    "document_page",
    "extraction_candidate",
    "timeline_event",
    "fact_conflict",
    "summary",
    "summary_statement",
    "physician_review",
    "namaste_mapping",
    "outbox_event",
    "integration_delivery",
)


async def _init_connection(conn: asyncpg.Connection) -> None:
    """Register the jsonb codec.

    With this codec registered, ``jsonb`` parameters must be passed as PYTHON
    OBJECTS — asyncpg calls the encoder itself. Passing a pre-serialised string
    double-encodes it into a jsonb *string*, which reads back as a string and
    silently breaks every predicate that expects an object. Use
    :func:`to_jsonb` at call sites so the intent is explicit.
    """
    await conn.set_type_codec(
        "jsonb",
        encoder=json.dumps,
        decoder=json.loads,
        schema="pg_catalog",
    )


async def _apply_principal(conn: asyncpg.Connection, principal: Principal) -> None:
    await conn.execute(
        """
        SELECT set_config('app.current_tenant',     $1, true),
               set_config('app.current_role',       $2, true),
               set_config('app.current_actor_id',   $3, true),
               set_config('app.current_patient_id', $4, true)
        """,
        str(principal.tenant_id),
        principal.role,
        str(principal.actor_id) if principal.actor_id else "",
        str(principal.patient_id) if principal.patient_id else "",
    )


def as_json(value: Any) -> str:
    """Serialise to a JSON STRING.

    For message-broker bodies and file payloads — NOT for jsonb parameters. See
    :func:`to_jsonb`.
    """
    return json.dumps(value, separators=(",", ":"), sort_keys=True, default=str)


def to_jsonb(value: Any) -> Any:
    """Prepare a value for a ``jsonb`` query parameter.

    Passes the object through, after a round-trip that applies ``default=str``
    so a UUID or datetime nested in a payload cannot raise inside asyncpg's
    encoder — where the failure would surface as an opaque serialisation error
    mid-transaction rather than at the call site.
    """
    return json.loads(as_json(value))
