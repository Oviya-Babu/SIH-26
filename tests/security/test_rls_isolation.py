"""Row Level Security, proven against a real PostgreSQL (CLAUDE.md §30, §57).

This file is the Phase 0 Definition of Done in executable form. §30 calls RLS
"the backstop that holds even if application code has a bug", and the only way
that claim means anything is to attack the database DIRECTLY — no API, no service
layer, no ORM — and confirm it refuses.

Every test here therefore issues raw SQL as ``medikiosk_app`` and asserts the
database itself withholds the rows.
"""

from __future__ import annotations

from uuid import uuid4

import asyncpg
import pytest

from medikiosk.db import PATIENT_DATA_TABLES
from tests.security.conftest import requires_db, set_context

pytestmark = [pytest.mark.security, requires_db]


class TestRolePosture:
    async def test_app_role_cannot_bypass_rls(self, app_conn):
        """If the API's role could bypass RLS, every other test here is theatre."""
        row = await app_conn.fetchrow(
            "SELECT current_user AS name, rolsuper, rolbypassrls "
            "FROM pg_roles WHERE rolname = current_user"
        )
        assert row["name"] == "medikiosk_app"
        assert row["rolsuper"] is False, "the API must not connect as a superuser"
        assert row["rolbypassrls"] is False, "the API role must be NOBYPASSRLS (§30)"

    async def test_rls_is_enabled_and_forced_on_every_patient_data_table(self, app_conn):
        """§30 [RED LINE]: RLS on every patient-data table, from the first migration.

        FORCE matters as much as ENABLE: without FORCE, a table owner silently
        bypasses its own policies.
        """
        rows = await app_conn.fetch(
            """
            SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity
              FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE n.nspname = 'public' AND c.relkind = 'r'
               AND c.relname = ANY($1::text[])
            """,
            list(PATIENT_DATA_TABLES),
        )
        found = {r["relname"] for r in rows}
        missing = set(PATIENT_DATA_TABLES) - found
        assert not missing, f"tables absent from the schema: {sorted(missing)}"

        unprotected = [
            r["relname"] for r in rows if not (r["relrowsecurity"] and r["relforcerowsecurity"])
        ]
        assert not unprotected, f"RLS not enabled+forced on: {sorted(unprotected)}"

    async def test_app_role_holds_no_update_or_delete_on_audit(self, app_conn):
        """§31: audit_event is append-only at the GRANT level, not by convention."""
        privileges = {
            r["privilege_type"]
            for r in await app_conn.fetch(
                """
                SELECT privilege_type FROM information_schema.table_privileges
                 WHERE table_name = 'audit_event' AND grantee = current_user
                """
            )
        }
        assert "INSERT" in privileges
        assert "SELECT" in privileges
        assert "UPDATE" not in privileges, "audit must not be updatable"
        assert "DELETE" not in privileges, "audit must not be deletable"


class TestCrossTenantIsolation:
    """The §64.8 demonstration, at the database layer."""

    async def test_tenant_context_shows_only_own_patients(self, app_conn, two_tenants):
        alpha, beta = two_tenants["alpha"], two_tenants["beta"]

        await set_context(app_conn, tenant_id=alpha["tenant_id"], role="physician")
        visible = {r["id"] for r in await app_conn.fetch("SELECT id FROM patient")}
        assert set(alpha["patients"]) <= visible
        assert not (set(beta["patients"]) & visible), "cross-tenant patient leak"

        await set_context(app_conn, tenant_id=beta["tenant_id"], role="physician")
        visible = {r["id"] for r in await app_conn.fetch("SELECT id FROM patient")}
        assert set(beta["patients"]) <= visible
        assert not (set(alpha["patients"]) & visible)

    async def test_direct_select_by_id_across_tenants_returns_nothing(
        self, app_conn, two_tenants
    ):
        """Knowing the UUID is not enough. This is the attack that matters."""
        alpha, beta = two_tenants["alpha"], two_tenants["beta"]
        await set_context(app_conn, tenant_id=alpha["tenant_id"], role="physician")

        for table, target in (
            ("patient", beta["patients"][0]),
            ("session", beta["sessions"][0]),
        ):
            row = await app_conn.fetchrow(
                f"SELECT * FROM {table} WHERE id = $1", target  # noqa: S608 — fixed literals
            )
            assert row is None, f"{table} leaked across tenants by id"

    async def test_clinical_facts_do_not_cross_tenants(self, app_conn, two_tenants):
        alpha, beta = two_tenants["alpha"], two_tenants["beta"]
        await set_context(app_conn, tenant_id=alpha["tenant_id"], role="physician")
        rows = await app_conn.fetch(
            "SELECT session_id FROM clinical_fact WHERE session_id = ANY($1::uuid[])",
            beta["sessions"],
        )
        assert rows == []

    async def test_cannot_insert_into_another_tenant(self, app_conn, two_tenants):
        """Writing across a tenant boundary must be refused, not merely filtered."""
        alpha, beta = two_tenants["alpha"], two_tenants["beta"]
        await set_context(app_conn, tenant_id=alpha["tenant_id"], role="physician")

        with pytest.raises(
            (asyncpg.InsufficientPrivilegeError, asyncpg.ForeignKeyViolationError)
        ):
            await app_conn.execute(
                """
                INSERT INTO clinical_fact
                    (tenant_id, session_id, patient_id, category, concept_code, concept_label,
                     value_normalized, confidence, source_type, respondent_id, provenance_ref)
                VALUES ($1, $2, $3, 'symptom', 'x', 'x', '{}'::jsonb, 1.0,
                        'patient_answer', $3, '{"method":"attack"}'::jsonb)
                """,
                beta["tenant_id"],
                beta["sessions"][0],
                beta["patients"][0],
            )

    async def test_update_cannot_reach_another_tenant(self, app_conn, two_tenants):
        alpha, beta = two_tenants["alpha"], two_tenants["beta"]
        await set_context(app_conn, tenant_id=alpha["tenant_id"], role="physician")
        result = await app_conn.execute(
            "UPDATE patient SET full_name = 'HACKED' WHERE id = $1", beta["patients"][0]
        )
        assert result == "UPDATE 0", "an update crossed a tenant boundary"

    async def test_delete_cannot_reach_another_tenant(self, app_conn, two_tenants):
        alpha, beta = two_tenants["alpha"], two_tenants["beta"]
        await set_context(app_conn, tenant_id=alpha["tenant_id"], role="physician")
        result = await app_conn.execute(
            "DELETE FROM session WHERE id = $1", beta["sessions"][0]
        )
        assert result == "DELETE 0"

    async def test_no_tenant_context_shows_nothing(self, app_conn, two_tenants):
        """A missing GUC must fail CLOSED.

        This is the bug class RLS exists to catch: a code path that forgets to
        set the tenant context must see an empty database, not everything.
        """
        await set_context(app_conn, tenant_id=None, role="physician")
        for table in ("patient", "session", "clinical_fact", "consent", "document"):
            rows = await app_conn.fetch(f"SELECT 1 FROM {table} LIMIT 1")  # noqa: S608
            assert rows == [], f"{table} was readable with no tenant context"

    async def test_bogus_tenant_context_shows_nothing(self, app_conn, two_tenants):
        await set_context(app_conn, tenant_id=uuid4(), role="physician")
        rows = await app_conn.fetch("SELECT 1 FROM patient LIMIT 1")
        assert rows == []


class TestPatientSelfAccess:
    """§30's ``patient_self_access`` policy — the §64.8 patient-token proof."""

    async def test_patient_sees_only_their_own_record(self, app_conn, two_tenants):
        alpha = two_tenants["alpha"]
        own, other = alpha["patients"][0], alpha["patients"][1]

        await set_context(
            app_conn, tenant_id=alpha["tenant_id"], role="patient", patient_id=own
        )
        visible = {r["id"] for r in await app_conn.fetch("SELECT id FROM patient")}
        assert visible == {own}, "a patient saw another patient in the same tenant"

    async def test_patient_cannot_read_another_patients_session(self, app_conn, two_tenants):
        alpha = two_tenants["alpha"]
        own, other_session = alpha["patients"][0], alpha["sessions"][1]

        await set_context(
            app_conn, tenant_id=alpha["tenant_id"], role="patient", patient_id=own
        )
        row = await app_conn.fetchrow("SELECT * FROM session WHERE id = $1", other_session)
        assert row is None

    async def test_patient_cannot_read_another_patients_facts(self, app_conn, two_tenants):
        alpha = two_tenants["alpha"]
        own = alpha["patients"][0]
        await set_context(
            app_conn, tenant_id=alpha["tenant_id"], role="patient", patient_id=own
        )
        rows = await app_conn.fetch(
            "SELECT patient_id FROM clinical_fact WHERE patient_id = $1", alpha["patients"][1]
        )
        assert rows == []

    async def test_staff_role_is_not_narrowed_by_patient_policy(self, app_conn, two_tenants):
        """A physician must see every patient in their tenant.

        The self-access policy applies to the patient role only; if it narrowed
        staff too, the physician dashboard would be empty.
        """
        alpha = two_tenants["alpha"]
        await set_context(app_conn, tenant_id=alpha["tenant_id"], role="physician")
        visible = {r["id"] for r in await app_conn.fetch("SELECT id FROM patient")}
        assert set(alpha["patients"]) <= visible

    async def test_patient_without_patient_id_sees_nothing(self, app_conn, two_tenants):
        """A patient-role context with no patient id must fail closed."""
        alpha = two_tenants["alpha"]
        await set_context(
            app_conn, tenant_id=alpha["tenant_id"], role="patient", patient_id=None
        )
        rows = await app_conn.fetch("SELECT 1 FROM patient LIMIT 1")
        assert rows == []


class TestAuditImmutability:
    """§31: append-only, hash-chained, enforced by the database."""

    async def test_audit_rows_cannot_be_updated(self, app_conn, two_tenants):
        alpha = two_tenants["alpha"]
        await set_context(
            app_conn, tenant_id=alpha["tenant_id"], role="physician",
            actor_id=alpha["physician_id"],
        )
        await app_conn.execute(
            """
            INSERT INTO audit_event (tenant_id, actor_id, actor_role, action,
                                     entity_type, entity_id, detail, prev_hash, row_hash)
            VALUES ($1, $2, 'physician', 'test.immutability', 'session', $3,
                    '{}'::jsonb, '', '')
            """,
            alpha["tenant_id"],
            alpha["physician_id"],
            alpha["sessions"][0],
        )
        audit_id = await app_conn.fetchval(
            "SELECT id FROM audit_event WHERE action = 'test.immutability' ORDER BY id DESC LIMIT 1"
        )
        assert audit_id is not None

        with pytest.raises(asyncpg.PostgresError) as exc:
            await app_conn.execute(
                "UPDATE audit_event SET action = 'tampered' WHERE id = $1", audit_id
            )
        assert "append-only" in str(exc.value).lower() or "permission" in str(exc.value).lower()

        with pytest.raises(asyncpg.PostgresError):
            await app_conn.execute("DELETE FROM audit_event WHERE id = $1", audit_id)

    async def test_hash_chain_is_computed_by_the_database(self, app_conn, two_tenants):
        """The application cannot write an unchained row even if it tries."""
        alpha = two_tenants["alpha"]
        await set_context(
            app_conn, tenant_id=alpha["tenant_id"], role="physician",
            actor_id=alpha["physician_id"],
        )
        # Deliberately supply empty hashes, as the application does.
        audit_id = await app_conn.fetchval(
            """
            INSERT INTO audit_event (tenant_id, actor_id, actor_role, action,
                                     entity_type, entity_id, detail, prev_hash, row_hash)
            VALUES ($1, $2, 'physician', 'test.chain', 'session', $3, '{}'::jsonb, '', '')
            RETURNING id
            """,
            alpha["tenant_id"],
            alpha["physician_id"],
            alpha["sessions"][0],
        )
        row = await app_conn.fetchrow(
            "SELECT prev_hash, row_hash FROM audit_event WHERE id = $1", audit_id
        )
        assert len(row["row_hash"]) == 64, "row_hash was not computed by the trigger"
        assert row["prev_hash"] != "", "prev_hash was not linked by the trigger"

    async def test_chain_verifies_intact(self, app_conn, two_tenants):
        alpha = two_tenants["alpha"]
        await set_context(app_conn, tenant_id=alpha["tenant_id"], role="security_officer")
        breaks = await app_conn.fetch("SELECT * FROM audit_chain_verify()")
        assert breaks == [], f"audit chain is broken: {breaks}"


class TestClinicalFactImmutability:
    """§13 [RED LINE]: content is immutable; corrections supersede."""

    async def test_fact_content_cannot_be_mutated(self, app_conn, two_tenants):
        alpha = two_tenants["alpha"]
        await set_context(app_conn, tenant_id=alpha["tenant_id"], role="physician")
        fact_id = await app_conn.fetchval(
            "SELECT id FROM clinical_fact WHERE session_id = $1", alpha["sessions"][0]
        )
        assert fact_id is not None

        with pytest.raises(asyncpg.PostgresError) as exc:
            await app_conn.execute(
                "UPDATE clinical_fact SET value_normalized = '{\"code\":\"tampered\"}'::jsonb "
                "WHERE id = $1",
                fact_id,
            )
        assert "immutable" in str(exc.value).lower()

    async def test_review_status_columns_remain_updatable(self, app_conn, two_tenants):
        """The immutability trigger must not block legitimate review updates."""
        alpha = two_tenants["alpha"]
        await set_context(app_conn, tenant_id=alpha["tenant_id"], role="physician")
        fact_id = await app_conn.fetchval(
            "SELECT id FROM clinical_fact WHERE session_id = $1", alpha["sessions"][0]
        )
        await app_conn.execute(
            "UPDATE clinical_fact SET verification_status = 'physician_verified' WHERE id = $1",
            fact_id,
        )
        status = await app_conn.fetchval(
            "SELECT verification_status FROM clinical_fact WHERE id = $1", fact_id
        )
        assert status == "physician_verified"


class TestReviewStateMachine:
    """§21 [RED LINE]: no path to exported skips approved — enforced in the DB."""

    async def test_draft_cannot_jump_straight_to_exported(self, app_conn, two_tenants):
        alpha = two_tenants["alpha"]
        await set_context(app_conn, tenant_id=alpha["tenant_id"], role="physician")

        with pytest.raises(asyncpg.PostgresError) as exc:
            await app_conn.execute(
                "UPDATE physician_review SET status = 'exported' WHERE session_id = $1",
                alpha["sessions"][0],
            )
        message = str(exc.value).lower()
        assert "illegal" in message or "transition" in message or "check" in message

    async def test_draft_cannot_jump_straight_to_approved(self, app_conn, two_tenants):
        alpha = two_tenants["alpha"]
        await set_context(app_conn, tenant_id=alpha["tenant_id"], role="physician")
        with pytest.raises(asyncpg.PostgresError):
            await app_conn.execute(
                """
                UPDATE physician_review
                   SET status = 'approved', approved_by = $2, approved_at = now()
                 WHERE session_id = $1
                """,
                alpha["sessions"][0],
                alpha["physician_id"],
            )

    async def test_legal_path_to_exported_is_permitted(self, app_conn, two_tenants):
        """The guard must permit the legitimate sequence, or it is just a brick."""
        alpha = two_tenants["alpha"]
        session_id = alpha["sessions"][1]
        await set_context(app_conn, tenant_id=alpha["tenant_id"], role="physician")

        await app_conn.execute(
            "UPDATE physician_review SET status = 'under_review', reviewer_id = $2 "
            "WHERE session_id = $1",
            session_id,
            alpha["physician_id"],
        )
        await app_conn.execute(
            """
            UPDATE physician_review
               SET status = 'approved', approved_by = $2, approved_at = now()
             WHERE session_id = $1
            """,
            session_id,
            alpha["physician_id"],
        )
        await app_conn.execute(
            "UPDATE physician_review SET status = 'exported', exported_at = now() "
            "WHERE session_id = $1",
            session_id,
        )
        status = await app_conn.fetchval(
            "SELECT status FROM physician_review WHERE session_id = $1", session_id
        )
        assert status == "exported"

    async def test_facts_are_sealed_after_export(self, app_conn, two_tenants):
        """§21: post-export clinical writes are refused at the storage layer."""
        alpha = two_tenants["alpha"]
        session_id = alpha["sessions"][1]
        await set_context(app_conn, tenant_id=alpha["tenant_id"], role="physician")

        # Drive the session to exported through the legal path.
        for statement, params in (
            ("UPDATE physician_review SET status='under_review', reviewer_id=$2 "
             "WHERE session_id=$1", (session_id, alpha["physician_id"])),
            ("UPDATE physician_review SET status='approved', approved_by=$2, "
             "approved_at=now() WHERE session_id=$1", (session_id, alpha["physician_id"])),
            ("UPDATE physician_review SET status='exported', exported_at=now() "
             "WHERE session_id=$1", (session_id,)),
        ):
            current = await app_conn.fetchval(
                "SELECT status FROM physician_review WHERE session_id = $1", session_id
            )
            if current == "exported":
                break
            await app_conn.execute(statement, *params)

        with pytest.raises(asyncpg.PostgresError) as exc:
            await app_conn.execute(
                """
                INSERT INTO clinical_fact
                    (tenant_id, session_id, patient_id, category, concept_code, concept_label,
                     value_normalized, confidence, source_type, respondent_id, provenance_ref)
                VALUES ($1, $2, $3, 'symptom', 'late', 'late', '{}'::jsonb, 1.0,
                        'patient_answer', $3, '{"method":"late"}'::jsonb)
                """,
                alpha["tenant_id"],
                session_id,
                alpha["patients"][1],
            )
        assert "sealed" in str(exc.value).lower()


class TestCaregiverAuthorityConstraints:
    """§6 [RED LINE]: a caregiver cannot self-declare consent authority."""

    async def test_default_basis_requires_patient_acknowledgment(self, app_conn, two_tenants):
        alpha = two_tenants["alpha"]
        await set_context(app_conn, tenant_id=alpha["tenant_id"], role="patient",
                          patient_id=alpha["patients"][0])
        with pytest.raises(asyncpg.CheckViolationError):
            await app_conn.execute(
                """
                INSERT INTO caregiver_authorization
                    (tenant_id, patient_id, caregiver_name, relationship, authority_basis)
                VALUES ($1, $2, 'Someone', 'child', 'patient_present_and_acknowledges')
                """,
                alpha["tenant_id"],
                alpha["patients"][0],
            )

    async def test_documented_authority_requires_document_and_witness(
        self, app_conn, two_tenants
    ):
        alpha = two_tenants["alpha"]
        await set_context(app_conn, tenant_id=alpha["tenant_id"], role="nurse")
        with pytest.raises(asyncpg.CheckViolationError):
            await app_conn.execute(
                """
                INSERT INTO caregiver_authorization
                    (tenant_id, patient_id, caregiver_name, relationship, authority_basis)
                VALUES ($1, $2, 'Someone', 'child', 'documented_guardianship')
                """,
                alpha["tenant_id"],
                alpha["patients"][0],
            )

    async def test_caregiver_with_acknowledgment_cannot_grant_consent(
        self, app_conn, two_tenants
    ):
        """The acknowledgment basis makes them a respondent, never a grantor."""
        alpha = two_tenants["alpha"]
        await set_context(app_conn, tenant_id=alpha["tenant_id"], role="nurse")
        auth_id = await app_conn.fetchval(
            """
            INSERT INTO caregiver_authorization
                (tenant_id, patient_id, caregiver_name, relationship, authority_basis,
                 patient_acknowledged_at, patient_ack_method)
            VALUES ($1, $2, 'Adult Child', 'child', 'patient_present_and_acknowledges',
                    now(), 'touch')
            RETURNING id
            """,
            alpha["tenant_id"],
            alpha["patients"][0],
        )

        with pytest.raises(asyncpg.PostgresError) as exc:
            await app_conn.execute(
                """
                INSERT INTO consent (tenant_id, patient_id, purpose, granted, notice_version,
                                     notice_language, grantor_type, grantor_caregiver_auth_id)
                VALUES ($1, $2, 'staff_access', true, 'v1', 'en', 'caregiver', $3)
                """,
                alpha["tenant_id"],
                alpha["patients"][0],
                auth_id,
            )
        assert "may not grant consent" in str(exc.value)

    async def test_caregiver_consent_requires_an_authorization(self, app_conn, two_tenants):
        alpha = two_tenants["alpha"]
        await set_context(app_conn, tenant_id=alpha["tenant_id"], role="nurse")
        with pytest.raises(asyncpg.PostgresError) as exc:
            await app_conn.execute(
                """
                INSERT INTO consent (tenant_id, patient_id, purpose, granted, notice_version,
                                     notice_language, grantor_type)
                VALUES ($1, $2, 'staff_access', true, 'v1', 'en', 'caregiver')
                """,
                alpha["tenant_id"],
                alpha["patients"][0],
            )
        assert "requires a caregiver_authorization" in str(exc.value)


class TestAadhaarRefusal:
    """§7.1 [RED LINE]: no raw Aadhaar, ever — including via a text column."""

    async def test_aadhaar_shaped_local_id_is_refused_by_the_database(
        self, app_conn, two_tenants
    ):
        alpha = two_tenants["alpha"]
        await set_context(app_conn, tenant_id=alpha["tenant_id"], role="nurse")
        with pytest.raises(asyncpg.CheckViolationError):
            await app_conn.execute(
                """
                INSERT INTO patient (tenant_id, hospital_local_id, full_name)
                VALUES ($1, '234567890123', 'Attempted Aadhaar')
                """,
                alpha["tenant_id"],
            )

    async def test_no_column_named_after_aadhaar_exists_anywhere(self, app_conn):
        rows = await app_conn.fetch(
            """
            SELECT table_name, column_name
              FROM information_schema.columns
             WHERE table_schema = 'public'
               AND (column_name ILIKE '%aadhaar%' OR column_name ILIKE '%aadhar%')
            """
        )
        assert rows == [], f"Aadhaar-named columns exist: {[dict(r) for r in rows]}"


class TestProvenanceConstraints:
    """§6, §13 [RED LINE]: no anonymous facts, no unprovenanced facts."""

    async def test_patient_answer_without_respondent_is_refused(self, app_conn, two_tenants):
        alpha = two_tenants["alpha"]
        await set_context(app_conn, tenant_id=alpha["tenant_id"], role="patient",
                          patient_id=alpha["patients"][0])
        with pytest.raises(asyncpg.CheckViolationError):
            await app_conn.execute(
                """
                INSERT INTO clinical_fact
                    (tenant_id, session_id, patient_id, category, concept_code, concept_label,
                     value_normalized, confidence, source_type, provenance_ref)
                VALUES ($1, $2, $3, 'symptom', 'x', 'x', '{}'::jsonb, 1.0,
                        'patient_answer', '{"method":"test"}'::jsonb)
                """,
                alpha["tenant_id"],
                alpha["sessions"][0],
                alpha["patients"][0],
            )

    async def test_empty_provenance_is_refused(self, app_conn, two_tenants):
        alpha = two_tenants["alpha"]
        await set_context(app_conn, tenant_id=alpha["tenant_id"], role="patient",
                          patient_id=alpha["patients"][0])
        with pytest.raises(asyncpg.CheckViolationError):
            await app_conn.execute(
                """
                INSERT INTO clinical_fact
                    (tenant_id, session_id, patient_id, category, concept_code, concept_label,
                     value_normalized, confidence, source_type, respondent_id, provenance_ref)
                VALUES ($1, $2, $3, 'symptom', 'x', 'x', '{}'::jsonb, 1.0,
                        'patient_answer', $3, '{}'::jsonb)
                """,
                alpha["tenant_id"],
                alpha["sessions"][0],
                alpha["patients"][0],
            )

    async def test_summary_statement_must_cite_something(self, app_conn, two_tenants):
        """§19 [RED LINE]: an uncited sentence cannot be persisted."""
        alpha = two_tenants["alpha"]
        await set_context(app_conn, tenant_id=alpha["tenant_id"], role="physician")
        summary_id = await app_conn.fetchval(
            """
            INSERT INTO summary (tenant_id, session_id, generation_mode)
            VALUES ($1, $2, 'llm_drafted')
            RETURNING id
            """,
            alpha["tenant_id"],
            alpha["sessions"][0],
        )
        with pytest.raises(asyncpg.CheckViolationError):
            await app_conn.execute(
                """
                INSERT INTO summary_statement
                    (tenant_id, summary_id, section, ordinal, text, citations)
                VALUES ($1, $2, 'presenting_complaint', 0,
                        'The patient probably has pneumonia.', ARRAY[]::uuid[])
                """,
                alpha["tenant_id"],
                summary_id,
            )
