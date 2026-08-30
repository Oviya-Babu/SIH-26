-- =============================================================================
-- MediKiosk migration 0002 — Consent, Caregiver Authority, Session, Protocol
--
-- [RED LINE §7.2] Internal MediKiosk consent and the ABDM network consent
-- artifact are structurally distinct tables. They are never merged, and neither
-- has a foreign key that would let one be mistaken for the other.
-- =============================================================================

-- =============================================================================
-- CONSENT — internal, MediKiosk-owned, purpose-specific, revocable (§7.2)
-- Gates everything MediKiosk itself does. Required for every session, always.
-- =============================================================================
CREATE TABLE consent (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id               UUID NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT,
    patient_id              UUID NOT NULL REFERENCES patient(id) ON DELETE RESTRICT,
    -- Granular purposes, each independently grantable and revocable.
    purpose                 TEXT NOT NULL CHECK (purpose IN (
                                'voice_capture',
                                'document_processing',
                                'ai_processing',
                                'staff_access',
                                'abdm_sharing_intent'
                            )),
    granted                 BOOLEAN NOT NULL,
    -- Which notice text/audio the person actually heard, versioned.
    notice_version          TEXT NOT NULL,
    notice_language         TEXT NOT NULL,
    audio_explained         BOOLEAN NOT NULL DEFAULT false,
    -- Who granted it. A caregiver may only appear here with documented authority.
    grantor_type            TEXT NOT NULL CHECK (grantor_type IN ('patient', 'caregiver')),
    grantor_caregiver_auth_id UUID,     -- FK added after caregiver_authorization
    granted_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at              TIMESTAMPTZ,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX consent_tenant_patient_idx ON consent (tenant_id, patient_id);
CREATE INDEX consent_active_idx ON consent (tenant_id, patient_id, purpose)
    WHERE revoked_at IS NULL;

ALTER TABLE consent ENABLE ROW LEVEL SECURITY;
ALTER TABLE consent FORCE ROW LEVEL SECURITY;
CREATE POLICY consent_tenant_isolation ON consent
    USING (tenant_id = app_current_tenant());
CREATE POLICY consent_patient_self ON consent
    USING (
        app_current_role() IS DISTINCT FROM 'patient'
        OR patient_id = app_current_patient_id()
    );

-- =============================================================================
-- ABDM_CONSENT_ARTIFACT_REF — a POINTER to an artifact owned by a registered
-- ABDM Consent Manager. MediKiosk never authors or self-certifies one (§23).
-- =============================================================================
CREATE TABLE abdm_consent_artifact_ref (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id               UUID NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT,
    patient_id              UUID NOT NULL REFERENCES patient(id) ON DELETE RESTRICT,
    -- Artifact id as issued by the Consent Manager. Sandbox CM id is 'sbx'.
    artifact_id             TEXT NOT NULL,
    consent_manager_id      TEXT NOT NULL,
    -- Honest labelling of provenance, surfaced in the UI (§23).
    environment             TEXT NOT NULL DEFAULT 'sandbox'
                            CHECK (environment IN ('sandbox', 'production')),
    status                  TEXT NOT NULL CHECK (status IN (
                                'requested', 'granted', 'denied', 'revoked', 'expired'
                            )),
    hiu_id                  TEXT,
    granted_at              TIMESTAMPTZ,
    expires_at              TIMESTAMPTZ,
    raw_artifact            JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, artifact_id)
);

ALTER TABLE abdm_consent_artifact_ref ENABLE ROW LEVEL SECURITY;
ALTER TABLE abdm_consent_artifact_ref FORCE ROW LEVEL SECURITY;
CREATE POLICY abdm_consent_ref_tenant_isolation ON abdm_consent_artifact_ref
    USING (tenant_id = app_current_tenant());

-- =============================================================================
-- CAREGIVER_AUTHORIZATION (§6)
-- A caregiver is always a respondent; a consent-grantor ONLY under documented
-- authority. [RED LINE] Blood relationship alone never grants consent authority,
-- and a caregiver can never self-declare authority.
-- =============================================================================
CREATE TABLE caregiver_authorization (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id               UUID NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT,
    patient_id              UUID NOT NULL REFERENCES patient(id) ON DELETE RESTRICT,
    caregiver_name          TEXT NOT NULL,
    relationship            TEXT NOT NULL,
    authority_basis         TEXT NOT NULL CHECK (authority_basis IN (
                                'patient_present_and_acknowledges',
                                'documented_guardianship',
                                'documented_medical_power_of_attorney'
                            )),
    -- Recorded acknowledgment, in the patient's own voice/tap, BEFORE the
    -- caregiver answers anything (§6).
    patient_acknowledged_at TIMESTAMPTZ,
    patient_ack_method      TEXT CHECK (patient_ack_method IN ('voice', 'touch')),
    -- Documented-authority paths are staff-witnessed at registration (§6).
    document_reference      TEXT,
    witnessed_by_user_id    UUID REFERENCES app_user(id) ON DELETE RESTRICT,
    verified_at             TIMESTAMPTZ,
    revoked_at              TIMESTAMPTZ,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- The default basis REQUIRES a recorded patient acknowledgment.
    CONSTRAINT caregiver_ack_required CHECK (
        authority_basis <> 'patient_present_and_acknowledges'
        OR (patient_acknowledged_at IS NOT NULL AND patient_ack_method IS NOT NULL)
    ),
    -- Documented-authority paths REQUIRE a document reference AND a staff witness.
    -- This is what makes self-declaration structurally impossible.
    CONSTRAINT caregiver_documented_authority_witnessed CHECK (
        authority_basis = 'patient_present_and_acknowledges'
        OR (document_reference IS NOT NULL
            AND witnessed_by_user_id IS NOT NULL
            AND verified_at IS NOT NULL)
    )
);

CREATE INDEX caregiver_auth_tenant_patient_idx
    ON caregiver_authorization (tenant_id, patient_id);

ALTER TABLE caregiver_authorization ENABLE ROW LEVEL SECURITY;
ALTER TABLE caregiver_authorization FORCE ROW LEVEL SECURITY;
CREATE POLICY caregiver_auth_tenant_isolation ON caregiver_authorization
    USING (tenant_id = app_current_tenant());

ALTER TABLE consent
    ADD CONSTRAINT consent_grantor_caregiver_fk
    FOREIGN KEY (grantor_caregiver_auth_id)
    REFERENCES caregiver_authorization(id) ON DELETE RESTRICT;

-- A caregiver-granted consent must point at a caregiver authorization whose
-- basis is a DOCUMENTED one. 'patient_present_and_acknowledges' makes the
-- caregiver a respondent only — the patient's own consent still governs (§6).
CREATE OR REPLACE FUNCTION consent_grantor_authority_check() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_basis TEXT;
BEGIN
    IF NEW.grantor_type = 'patient' THEN
        IF NEW.grantor_caregiver_auth_id IS NOT NULL THEN
            RAISE EXCEPTION
                'patient-granted consent must not reference a caregiver authorization';
        END IF;
        RETURN NEW;
    END IF;

    IF NEW.grantor_caregiver_auth_id IS NULL THEN
        RAISE EXCEPTION
            'caregiver-granted consent requires a caregiver_authorization (CLAUDE.md §6)';
    END IF;

    SELECT authority_basis INTO v_basis
      FROM caregiver_authorization
     WHERE id = NEW.grantor_caregiver_auth_id
       AND patient_id = NEW.patient_id
       AND revoked_at IS NULL;

    IF v_basis IS NULL THEN
        RAISE EXCEPTION
            'caregiver_authorization not found, revoked, or patient mismatch';
    END IF;

    IF v_basis NOT IN ('documented_guardianship',
                       'documented_medical_power_of_attorney') THEN
        RAISE EXCEPTION
            'caregiver may not grant consent under basis % (CLAUDE.md §6 [RED LINE])', v_basis;
    END IF;

    RETURN NEW;
END
$$;

CREATE TRIGGER consent_grantor_authority_trg
    BEFORE INSERT OR UPDATE ON consent
    FOR EACH ROW EXECUTE FUNCTION consent_grantor_authority_check();

-- =============================================================================
-- PROTOCOL_VERSION — governed, versioned clinical content (§10)
-- Content is authored/reviewed through the CI governance gate (§46, §61); this
-- table is the runtime registry of what a tenant is permitted to load.
-- =============================================================================
CREATE TABLE protocol_version (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    protocol_family         TEXT NOT NULL,
    version                 TEXT NOT NULL,
    display_name            TEXT NOT NULL,
    -- sha256 of the governed content file, so a runtime load can be proven to
    -- match what governance actually approved.
    content_checksum        TEXT NOT NULL,
    status                  TEXT NOT NULL DEFAULT 'active' CHECK (status IN (
                                'draft', 'in_review', 'active', 'deprecated'
                            )),
    governance_reviewer     TEXT,
    approved_at             TIMESTAMPTZ,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (protocol_family, version)
);

-- Protocol content is not patient data and is shared across tenants; it is
-- readable by any authenticated principal, writable by no application role.
GRANT SELECT ON protocol_version TO medikiosk_app;

-- =============================================================================
-- SESSION — one kiosk encounter
-- =============================================================================
CREATE TABLE session (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id               UUID NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT,
    patient_id              UUID NOT NULL REFERENCES patient(id) ON DELETE RESTRICT,
    department_id           UUID NOT NULL REFERENCES department(id) ON DELETE RESTRICT,
    device_id               UUID REFERENCES device(id) ON DELETE SET NULL,
    protocol_family         TEXT NOT NULL,
    protocol_version        TEXT NOT NULL,
    language                TEXT NOT NULL DEFAULT 'en',

    -- Who is answering. A caregiver respondent is recorded here AND on every fact.
    respondent_type         TEXT NOT NULL DEFAULT 'patient'
                            CHECK (respondent_type IN ('patient', 'caregiver', 'staff')),
    caregiver_auth_id       UUID REFERENCES caregiver_authorization(id) ON DELETE RESTRICT,
    assigned_physician_id   UUID REFERENCES app_user(id) ON DELETE SET NULL,

    status                  TEXT NOT NULL DEFAULT 'in_progress' CHECK (status IN (
                                'in_progress',
                                'escalated_to_staff',   -- red-flag fast path (§14)
                                'awaiting_confirmation',
                                'completed',
                                'abandoned'
                            )),
    -- Set when the red-flag fast path overrides the required set (§14)
    fast_path_active        BOOLEAN NOT NULL DEFAULT false,
    fast_path_activated_at  TIMESTAMPTZ,

    completeness            NUMERIC(4,3) NOT NULL DEFAULT 0,
    started_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_activity_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    submitted_at            TIMESTAMPTZ,
    -- Proof that the synchronous purge of transient session state ran (§38)
    transient_purged_at     TIMESTAMPTZ,

    CONSTRAINT session_caregiver_requires_auth CHECK (
        respondent_type <> 'caregiver' OR caregiver_auth_id IS NOT NULL
    )
);

CREATE INDEX session_tenant_status_idx ON session (tenant_id, status);
CREATE INDEX session_tenant_dept_idx ON session (tenant_id, department_id, status);
CREATE INDEX session_patient_idx ON session (tenant_id, patient_id);

ALTER TABLE session ENABLE ROW LEVEL SECURITY;
ALTER TABLE session FORCE ROW LEVEL SECURITY;
CREATE POLICY session_tenant_isolation ON session
    USING (tenant_id = app_current_tenant());
CREATE POLICY session_patient_self ON session
    USING (
        app_current_role() IS DISTINCT FROM 'patient'
        OR patient_id = app_current_patient_id()
    );

-- =============================================================================
-- SESSION_ANSWER — the raw answer stream, distinct from derived clinical facts.
-- Every answer carries its respondent. [RED LINE §6] no anonymous answers.
-- =============================================================================
CREATE TABLE session_answer (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id               UUID NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT,
    session_id              UUID NOT NULL REFERENCES session(id) ON DELETE CASCADE,
    field_id                TEXT NOT NULL,
    value_raw               TEXT,
    value_normalized        JSONB NOT NULL,
    input_method            TEXT NOT NULL CHECK (input_method IN ('voice', 'touch', 'text')),
    confidence              NUMERIC(4,3) NOT NULL DEFAULT 1.0,
    confirmed               BOOLEAN NOT NULL DEFAULT false,
    respondent_type         TEXT NOT NULL CHECK (respondent_type IN ('patient', 'caregiver', 'staff')),
    respondent_id           UUID NOT NULL,
    respondent_relationship TEXT,
    -- Distinguishes "we never asked" from "the patient did not know" (§14.4)
    skip_reason             TEXT CHECK (skip_reason IN (
                                'not_answered',
                                'not_asked_due_to_emergency_escalation',
                                'patient_declined',
                                'patient_unsure'
                            )),
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    superseded_by           UUID REFERENCES session_answer(id) ON DELETE SET NULL
);

CREATE INDEX session_answer_session_idx ON session_answer (tenant_id, session_id);
CREATE UNIQUE INDEX session_answer_current_idx
    ON session_answer (session_id, field_id) WHERE superseded_by IS NULL;

ALTER TABLE session_answer ENABLE ROW LEVEL SECURITY;
ALTER TABLE session_answer FORCE ROW LEVEL SECURITY;
CREATE POLICY session_answer_tenant_isolation ON session_answer
    USING (tenant_id = app_current_tenant());

-- =============================================================================
-- UPLOAD_TOKEN — QR-to-phone handoff (§9). Upload-only, single session, TTL.
-- =============================================================================
CREATE TABLE upload_token (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id               UUID NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT,
    session_id              UUID NOT NULL REFERENCES session(id) ON DELETE CASCADE,
    -- SHA-256 of the token. The token itself is only ever in the QR code.
    token_hash              TEXT NOT NULL UNIQUE,
    -- Scope is literally upload-only; there is no read scope to widen to.
    scope                   TEXT NOT NULL DEFAULT 'document_upload'
                            CHECK (scope = 'document_upload'),
    respondent_type         TEXT NOT NULL CHECK (respondent_type IN ('patient', 'caregiver', 'staff')),
    respondent_id           UUID NOT NULL,
    respondent_relationship TEXT,
    expires_at              TIMESTAMPTZ NOT NULL,
    revoked_at              TIMESTAMPTZ,
    use_count               INTEGER NOT NULL DEFAULT 0,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX upload_token_session_idx ON upload_token (tenant_id, session_id);

ALTER TABLE upload_token ENABLE ROW LEVEL SECURITY;
ALTER TABLE upload_token FORCE ROW LEVEL SECURITY;
CREATE POLICY upload_token_tenant_isolation ON upload_token
    USING (tenant_id = app_current_tenant());

GRANT SELECT, INSERT, UPDATE, DELETE ON
    consent, abdm_consent_artifact_ref, caregiver_authorization,
    session, session_answer, upload_token
    TO medikiosk_app;
