"""Fixtures for database-backed security tests.

These tests require a real PostgreSQL with the migrations applied, because the
guarantee under test is a DATABASE guarantee. Mocking RLS would test nothing:
§30 calls RLS the backstop that holds even when application code has a bug, and
that claim is only meaningful against the real engine.

    scripts/local_pg.sh start
    MEDIKIOSK_MIGRATION_DSN=... python scripts/migrate.py
    MEDIKIOSK_TEST_DATABASE_URL=postgresql://medikiosk_app:medikiosk_app@127.0.0.1:55432/medikiosk \\
        pytest tests/security -m security
"""

from __future__ import annotations

import os
from uuid import UUID, uuid4

import asyncpg
import pytest
import pytest_asyncio

from medikiosk.db import Principal

APP_DSN = os.environ.get("MEDIKIOSK_TEST_DATABASE_URL")
OWNER_DSN = os.environ.get("MEDIKIOSK_TEST_OWNER_DATABASE_URL") or os.environ.get(
    "MEDIKIOSK_MIGRATION_DSN"
)

requires_db = pytest.mark.skipif(
    not APP_DSN,
    reason="set MEDIKIOSK_TEST_DATABASE_URL to run database-backed security tests",
)


@pytest_asyncio.fixture
async def app_conn():
    """A connection as ``medikiosk_app`` — the NOBYPASSRLS role the API uses."""
    conn = await asyncpg.connect(APP_DSN)
    try:
        yield conn
    finally:
        await conn.close()


@pytest_asyncio.fixture
async def owner_conn():
    """A connection as the owner, for arranging fixtures RLS would otherwise hide."""
    if not OWNER_DSN:
        pytest.skip("owner DSN not configured")
    conn = await asyncpg.connect(OWNER_DSN)
    try:
        yield conn
    finally:
        await conn.close()


async def set_context(
    conn: asyncpg.Connection,
    *,
    tenant_id: UUID | None,
    role: str,
    patient_id: UUID | None = None,
    actor_id: UUID | None = None,
) -> None:
    """Set the session GUCs exactly as :meth:`Database.transaction` does."""
    await conn.execute(
        """
        SELECT set_config('app.current_tenant',     $1, false),
               set_config('app.current_role',       $2, false),
               set_config('app.current_actor_id',   $3, false),
               set_config('app.current_patient_id', $4, false)
        """,
        str(tenant_id) if tenant_id else "",
        role,
        str(actor_id) if actor_id else "",
        str(patient_id) if patient_id else "",
    )


@pytest_asyncio.fixture
async def two_tenants(owner_conn):
    """Two tenants, each with a department, a patient and a session.

    Created as the owner so the arrangement itself is not subject to the policy
    under test — otherwise a passing test could just mean the setup failed.
    """
    suffix = uuid4().hex[:8]
    created: dict[str, dict] = {}

    for name in ("alpha", "beta"):
        tenant_id = await owner_conn.fetchval(
            "INSERT INTO tenant (slug, display_name) VALUES ($1, $2) RETURNING id",
            f"rls-test-{name}-{suffix}",
            f"RLS Test {name.title()} {suffix}",
        )
        department_id = await owner_conn.fetchval(
            """
            INSERT INTO department (tenant_id, code, display_name, protocol_family)
            VALUES ($1, 'GEN-MED', 'General Medicine', 'general_medicine')
            RETURNING id
            """,
            tenant_id,
        )
        other_department_id = await owner_conn.fetchval(
            """
            INSERT INTO department (tenant_id, code, display_name, protocol_family)
            VALUES ($1, 'AYUSH-AYU', 'Ayurveda', 'ayush_ayurveda')
            RETURNING id
            """,
            tenant_id,
        )
        await owner_conn.execute(
            """
            INSERT INTO tenant_protocol_config (tenant_id, protocol_family, active_version)
            VALUES ($1, 'general_medicine', 'v1'), ($1, 'ayush_ayurveda', 'v1')
            """,
            tenant_id,
        )
        physician_id = await owner_conn.fetchval(
            """
            INSERT INTO app_user (tenant_id, subject, username, display_name, role,
                                  assigned_department_id, mfa_enrolled)
            VALUES ($1, $2, 'phys', 'Physician', 'physician', $3, true)
            RETURNING id
            """,
            tenant_id,
            f"subj-phys-{name}-{suffix}",
            department_id,
        )
        nurse_id = await owner_conn.fetchval(
            """
            INSERT INTO app_user (tenant_id, subject, username, display_name, role,
                                  assigned_department_id)
            VALUES ($1, $2, 'nurse', 'Nurse', 'nurse', $3)
            RETURNING id
            """,
            tenant_id,
            f"subj-nurse-{name}-{suffix}",
            department_id,
        )

        patients: list[UUID] = []
        sessions: list[UUID] = []
        for index in (1, 2):
            patient_id = await owner_conn.fetchval(
                """
                INSERT INTO patient (tenant_id, hospital_local_id, full_name,
                                     year_of_birth, gender, preferred_language)
                VALUES ($1, $2, $3, 1980, 'other', 'en')
                RETURNING id
                """,
                tenant_id,
                f"LOCAL-{name}-{suffix}-{index}",
                f"Synthetic Patient {name}{index}",
            )
            session_id = await owner_conn.fetchval(
                """
                INSERT INTO session (tenant_id, patient_id, department_id, protocol_family,
                                     protocol_version, language)
                VALUES ($1, $2, $3, 'general_medicine', 'v1', 'en')
                RETURNING id
                """,
                tenant_id,
                patient_id,
                department_id,
            )
            await owner_conn.execute(
                """
                INSERT INTO physician_review (tenant_id, session_id, status)
                VALUES ($1, $2, 'draft')
                """,
                tenant_id,
                session_id,
            )
            await owner_conn.execute(
                """
                INSERT INTO clinical_fact
                    (tenant_id, session_id, patient_id, category, concept_code, concept_label,
                     value_normalized, confidence, source_type, respondent_id, provenance_ref)
                VALUES ($1, $2, $3, 'chief_complaint', 'chief_complaint',
                        'gm.cc.primary_complaint', '{"code":"fever"}'::jsonb, 1.0,
                        'patient_answer', $3, '{"method":"test"}'::jsonb)
                """,
                tenant_id,
                session_id,
                patient_id,
            )
            patients.append(patient_id)
            sessions.append(session_id)

        created[name] = {
            "tenant_id": tenant_id,
            "department_id": department_id,
            "other_department_id": other_department_id,
            "physician_id": physician_id,
            "nurse_id": nurse_id,
            "patients": patients,
            "sessions": sessions,
        }

    yield created

    # Teardown in strict reverse-dependency order. The clinical schema uses
    # ON DELETE RESTRICT deliberately (§13: a fact is never orphaned or silently
    # cascaded away), so a test fixture has to unwind it explicitly.
    ordered_tables = (
        "summary_statement",
        "summary",
        "timeline_event",
        "fact_conflict",
        "namaste_mapping",
        "extraction_candidate",
        "document_page",
        "document",
        "clinical_fact",
        "physician_review",
        "red_flag_alert",
        "red_flag_evaluation",
        "session_answer",
        "upload_token",
        "integration_delivery",
        "outbox_event",
        "session",
        "consent",
        "abdm_consent_artifact_ref",
        "caregiver_authorization",
        "patient",
        "app_user",
        "tenant_protocol_config",
        "device",
        "department",
    )
    for data in created.values():
        for table in ordered_tables:
            await owner_conn.execute(
                f"DELETE FROM {table} WHERE tenant_id = $1", data["tenant_id"]  # noqa: S608
            )
        # audit_event is deliberately NOT cleaned up: it is append-only, and the
        # trigger refuses a DELETE even from the owner (§31). Audit rows from a
        # test run are immutable evidence that the test ran, which is the correct
        # behaviour — a test fixture must not be able to erase an audit trail.
        await owner_conn.execute("DELETE FROM tenant WHERE id = $1", data["tenant_id"])


def principal_for(data: dict, role: str, **overrides) -> Principal:
    return Principal(
        tenant_id=data["tenant_id"],
        role=role,
        actor_id=overrides.get("actor_id", data.get("physician_id")),
        department_id=overrides.get("department_id", data["department_id"]),
        patient_id=overrides.get("patient_id"),
        session_id=overrides.get("session_id"),
        mfa_satisfied=overrides.get("mfa_satisfied", True),
    )
