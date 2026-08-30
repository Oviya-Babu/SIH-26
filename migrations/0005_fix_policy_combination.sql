-- =============================================================================
-- MediKiosk migration 0005 — CRITICAL RLS CORRECTION
--
-- Fixes two defects found by tests/security/test_rls_isolation.py. Both were
-- silent: the schema looked correct, `\d` showed policies on every table, and
-- neither defect produced an error — they simply failed to protect anything.
--
-- ---------------------------------------------------------------------------
-- DEFECT 1 — permissive policies are OR-ed, not AND-ed.
--
-- Migrations 0001-0003 created, on five tables, two PERMISSIVE policies:
--
--     POLICY tenant_isolation     USING (tenant_id = app_current_tenant())
--     POLICY patient_self_access  USING (app_current_role() <> 'patient' OR ...)
--
-- PostgreSQL combines multiple permissive policies with OR. For any staff role,
-- `patient_self_access` evaluates TRUE for EVERY row, so the disjunction was
-- TRUE for every row in every tenant. Tenant isolation was effectively absent
-- on patient, consent, session, clinical_fact and document — the five tables it
-- mattered most on.
--
-- The fix: keep tenant isolation as the single PERMISSIVE policy (it GRANTS
-- access within a tenant) and make patient-self a RESTRICTIVE policy (it further
-- NARROWS that grant). Restrictive policies are AND-ed, which is the semantics
-- §30 actually requires.
--
-- ---------------------------------------------------------------------------
-- DEFECT 2 — array_length() on an empty array returns NULL, not 0.
--
--     CHECK (array_length(citations, 1) >= 1)
--
-- With `citations = ARRAY[]::uuid[]`, array_length is NULL, `NULL >= 1` is NULL,
-- and a CHECK constraint treats NULL as SATISFIED. The §19 [RED LINE] "no
-- uncited sentence may be persisted" was therefore unenforced for exactly the
-- input it existed to reject.
--
-- ---------------------------------------------------------------------------
-- Applied migrations are immutable (scripts/migrate.py refuses edits), so the
-- correction ships as its own migration. That is also the honest record: the
-- defect existed, and this is when it was fixed.
-- =============================================================================

-- -----------------------------------------------------------------------------
-- DEFECT 1: convert patient-self policies from PERMISSIVE to RESTRICTIVE.
--
-- Read the pair together as: "you may see rows in your own tenant (permissive),
-- AND if you are a patient you may only see your own (restrictive)."
-- -----------------------------------------------------------------------------

-- patient ---------------------------------------------------------------------
DROP POLICY IF EXISTS patient_self_access ON patient;
CREATE POLICY patient_self_access ON patient
    AS RESTRICTIVE
    USING (
        app_current_role() IS DISTINCT FROM 'patient'
        OR id = app_current_patient_id()
    )
    WITH CHECK (
        app_current_role() IS DISTINCT FROM 'patient'
        OR id = app_current_patient_id()
    );

DROP POLICY IF EXISTS patient_tenant_isolation ON patient;
CREATE POLICY patient_tenant_isolation ON patient
    USING (tenant_id = app_current_tenant())
    WITH CHECK (tenant_id = app_current_tenant());

-- consent ---------------------------------------------------------------------
DROP POLICY IF EXISTS consent_patient_self ON consent;
CREATE POLICY consent_patient_self ON consent
    AS RESTRICTIVE
    USING (
        app_current_role() IS DISTINCT FROM 'patient'
        OR patient_id = app_current_patient_id()
    )
    WITH CHECK (
        app_current_role() IS DISTINCT FROM 'patient'
        OR patient_id = app_current_patient_id()
    );

DROP POLICY IF EXISTS consent_tenant_isolation ON consent;
CREATE POLICY consent_tenant_isolation ON consent
    USING (tenant_id = app_current_tenant())
    WITH CHECK (tenant_id = app_current_tenant());

-- session ---------------------------------------------------------------------
DROP POLICY IF EXISTS session_patient_self ON session;
CREATE POLICY session_patient_self ON session
    AS RESTRICTIVE
    USING (
        app_current_role() IS DISTINCT FROM 'patient'
        OR patient_id = app_current_patient_id()
    )
    WITH CHECK (
        app_current_role() IS DISTINCT FROM 'patient'
        OR patient_id = app_current_patient_id()
    );

DROP POLICY IF EXISTS session_tenant_isolation ON session;
CREATE POLICY session_tenant_isolation ON session
    USING (tenant_id = app_current_tenant())
    WITH CHECK (tenant_id = app_current_tenant());

-- clinical_fact ---------------------------------------------------------------
DROP POLICY IF EXISTS patient_self_access ON clinical_fact;
CREATE POLICY patient_self_access ON clinical_fact
    AS RESTRICTIVE
    USING (
        app_current_role() IS DISTINCT FROM 'patient'
        OR patient_id = app_current_patient_id()
    )
    WITH CHECK (
        app_current_role() IS DISTINCT FROM 'patient'
        OR patient_id = app_current_patient_id()
    );

DROP POLICY IF EXISTS tenant_isolation ON clinical_fact;
CREATE POLICY tenant_isolation ON clinical_fact
    USING (tenant_id = app_current_tenant())
    WITH CHECK (tenant_id = app_current_tenant());

-- document --------------------------------------------------------------------
DROP POLICY IF EXISTS document_patient_self ON document;
CREATE POLICY document_patient_self ON document
    AS RESTRICTIVE
    USING (
        app_current_role() IS DISTINCT FROM 'patient'
        OR patient_id = app_current_patient_id()
    )
    WITH CHECK (
        app_current_role() IS DISTINCT FROM 'patient'
        OR patient_id = app_current_patient_id()
    );

DROP POLICY IF EXISTS document_tenant_isolation ON document;
CREATE POLICY document_tenant_isolation ON document
    USING (tenant_id = app_current_tenant())
    WITH CHECK (tenant_id = app_current_tenant());

-- -----------------------------------------------------------------------------
-- Every remaining patient-data table has exactly ONE permissive policy for the
-- app role, so the OR defect cannot apply. Add explicit WITH CHECK clauses so
-- writes are constrained by the same expression as reads, rather than relying on
-- PostgreSQL's implicit fallback.
-- -----------------------------------------------------------------------------
DO $$
DECLARE
    t TEXT;
    policy_name TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'department', 'tenant_protocol_config', 'app_user', 'device',
        'abdm_consent_artifact_ref', 'caregiver_authorization',
        'session_answer', 'upload_token', 'red_flag_evaluation', 'red_flag_alert',
        'document_page', 'extraction_candidate', 'timeline_event', 'fact_conflict',
        'summary', 'summary_statement', 'physician_review', 'namaste_mapping'
    ]
    LOOP
        policy_name := t || '_tenant_write_check';
        EXECUTE format(
            'DROP POLICY IF EXISTS %I ON %I', policy_name, t
        );
        EXECUTE format(
            'CREATE POLICY %I ON %I AS RESTRICTIVE '
            'USING (tenant_id = app_current_tenant()) '
            'WITH CHECK (tenant_id = app_current_tenant())',
            policy_name, t
        );
    END LOOP;
END
$$;

-- -----------------------------------------------------------------------------
-- DEFECT 2: make the citation-required constraint actually reject an empty array.
--
-- Remediation of rows admitted while the constraint was ineffective: any
-- summary_statement with no citations is deleted. This is safe and correct —
-- summary statements are a REGENERABLE draft, not the clinical record. The
-- clinical record is clinical_fact, which is untouched here, and the draft is
-- rebuilt by POST /v1/summaries/{id}/regenerate.
--
-- An uncited statement is precisely what §19 forbids, so leaving one in place to
-- preserve it would preserve the defect.
-- -----------------------------------------------------------------------------
DO $$
DECLARE
    removed BIGINT;
BEGIN
    WITH deleted AS (
        DELETE FROM summary_statement
         WHERE COALESCE(array_length(citations, 1), 0) = 0
        RETURNING 1
    )
    SELECT count(*) INTO removed FROM deleted;

    IF removed > 0 THEN
        RAISE NOTICE
            'removed % uncited summary statement(s) admitted while the citation '
            'constraint was ineffective; regenerate affected drafts', removed;
    END IF;
END
$$;

ALTER TABLE summary_statement
    DROP CONSTRAINT IF EXISTS summary_statement_must_cite;

ALTER TABLE summary_statement
    ADD CONSTRAINT summary_statement_must_cite
    CHECK (COALESCE(array_length(citations, 1), 0) >= 1);

-- -----------------------------------------------------------------------------
-- A regression guard, so this class of defect cannot silently return.
--
-- Counts PERMISSIVE policies per table for the app role. More than one on a
-- patient-data table means an OR is combining them, which is almost always a
-- mistake — the second policy widens access instead of narrowing it.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION rls_permissive_policy_audit()
RETURNS TABLE (table_name TEXT, permissive_policies BIGINT, policy_names TEXT[])
LANGUAGE sql STABLE AS $$
    SELECT p.tablename::TEXT,
           count(*),
           array_agg(p.policyname::TEXT ORDER BY p.policyname)
      FROM pg_policies p
     WHERE p.schemaname = 'public'
       AND p.permissive = 'PERMISSIVE'
       -- Role-scoped policies (e.g. the relay's) are a separate grant path, not
       -- an accidental widening of the app role's access.
       AND (p.roles = '{public}' OR p.roles IS NULL)
     GROUP BY p.tablename
    HAVING count(*) > 1
     ORDER BY p.tablename
$$;

GRANT EXECUTE ON FUNCTION rls_permissive_policy_audit()
    TO medikiosk_app, medikiosk_audit;
