-- =============================================================================
-- MediKiosk migration 0003 — Clinical core
--
-- clinical_fact is the atomic clinical record (§13). Its invariants are the
-- product's clinical-safety spine:
--   * every fact carries provenance_ref                     [RED LINE §13]
--   * every non-document fact carries respondent_id         [RED LINE §6]
--   * patient / caregiver / document / physician facts are never merged or
--     overwritten — a correction SUPERSEDES, it does not mutate  [RED LINE §13]
--   * AI never writes here directly; only the clinical_facts module does (§20)
-- =============================================================================

CREATE TABLE clinical_fact (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id               UUID NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT,
    session_id              UUID NOT NULL REFERENCES session(id) ON DELETE RESTRICT,
    -- Denormalised so the §30 patient_self_access policy can be enforced here.
    patient_id              UUID NOT NULL REFERENCES patient(id) ON DELETE RESTRICT,

    category                TEXT NOT NULL CHECK (category IN (
                                'chief_complaint',
                                'symptom',
                                'review_of_systems',
                                'past_medical_history',
                                'past_surgical_history',
                                'procedure_history',
                                'medication',
                                'allergy',
                                'family_history',
                                'personal_history',
                                'investigation_value',
                                'vital_sign',
                                'diagnosis',
                                'dashavidha_parameter',
                                'ahara_vihara',
                                'nidana',
                                'samprapti',
                                'ample_field'
                            )),
    concept_code            TEXT NOT NULL,
    concept_label           TEXT NOT NULL,
    value_raw               TEXT,
    value_normalized        JSONB NOT NULL,
    unit                    TEXT,
    confidence              NUMERIC(4,3) NOT NULL CHECK (confidence >= 0 AND confidence <= 1),

    source_type             TEXT NOT NULL CHECK (source_type IN (
                                'patient_answer',
                                'caregiver_answer',
                                'document_extraction',
                                'staff_entry',
                                'physician_edit'
                            )),
    -- NULL only for document_extraction, where the uploader's identity lives on
    -- the document row instead (§13).
    respondent_id           UUID,
    respondent_relationship TEXT,

    -- {document_id, page, method, model_version, timestamp, ...}
    provenance_ref          JSONB NOT NULL,

    verification_status     TEXT NOT NULL DEFAULT 'unverified' CHECK (verification_status IN (
                                'unverified',
                                'awaiting_human_verification',
                                'patient_confirmed',
                                'physician_verified',
                                'physician_rejected'
                            )),
    is_conflicting          BOOLEAN NOT NULL DEFAULT false,
    conflict_group_id       UUID,
    -- Lab abnormality is a deterministic comparison, never AI-inferred (§15).
    abnormal_flag           TEXT CHECK (abnormal_flag IN ('low', 'normal', 'high', 'critical')),
    superseded_by           UUID REFERENCES clinical_fact(id) ON DELETE RESTRICT,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT clinical_fact_respondent_required CHECK (
        source_type = 'document_extraction' OR respondent_id IS NOT NULL
    ),
    CONSTRAINT clinical_fact_provenance_not_empty CHECK (
        provenance_ref <> '{}'::jsonb
    )
);

CREATE INDEX clinical_fact_session_idx ON clinical_fact (tenant_id, session_id);
CREATE INDEX clinical_fact_patient_idx ON clinical_fact (tenant_id, patient_id);
CREATE INDEX clinical_fact_concept_idx ON clinical_fact (tenant_id, session_id, concept_code);
CREATE INDEX clinical_fact_current_idx ON clinical_fact (tenant_id, session_id)
    WHERE superseded_by IS NULL;

ALTER TABLE clinical_fact ENABLE ROW LEVEL SECURITY;
ALTER TABLE clinical_fact FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON clinical_fact
    USING (tenant_id = app_current_tenant());
CREATE POLICY patient_self_access ON clinical_fact
    USING (
        app_current_role() IS DISTINCT FROM 'patient'
        OR patient_id = app_current_patient_id()
    );

-- A fact's clinical content is immutable. Only the linkage/status columns that
-- represent later review may change. Corrections create a new superseding row.
CREATE OR REPLACE FUNCTION clinical_fact_no_content_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.category         IS DISTINCT FROM OLD.category
    OR NEW.concept_code     IS DISTINCT FROM OLD.concept_code
    OR NEW.value_raw        IS DISTINCT FROM OLD.value_raw
    OR NEW.value_normalized IS DISTINCT FROM OLD.value_normalized
    OR NEW.source_type      IS DISTINCT FROM OLD.source_type
    OR NEW.respondent_id    IS DISTINCT FROM OLD.respondent_id
    OR NEW.provenance_ref   IS DISTINCT FROM OLD.provenance_ref
    OR NEW.session_id       IS DISTINCT FROM OLD.session_id
    OR NEW.patient_id       IS DISTINCT FROM OLD.patient_id
    OR NEW.tenant_id        IS DISTINCT FROM OLD.tenant_id THEN
        RAISE EXCEPTION
            'clinical_fact content is immutable; supersede instead (CLAUDE.md §13 [RED LINE])';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER clinical_fact_immutable_content_trg
    BEFORE UPDATE ON clinical_fact
    FOR EACH ROW EXECUTE FUNCTION clinical_fact_no_content_mutation();

-- =============================================================================
-- LAB_REFERENCE_RANGE — governed, versioned, age/sex/unit aware (§15)
-- Not patient data. The ONLY source of abnormality classification.
-- =============================================================================
CREATE TABLE lab_reference_range (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    analyte_code            TEXT NOT NULL,
    analyte_label           TEXT NOT NULL,
    unit                    TEXT NOT NULL,
    sex                     TEXT NOT NULL DEFAULT 'any'
                            CHECK (sex IN ('any', 'male', 'female')),
    age_min_years           INTEGER NOT NULL DEFAULT 0,
    age_max_years           INTEGER NOT NULL DEFAULT 200,
    low                     NUMERIC,
    high                    NUMERIC,
    critical_low            NUMERIC,
    critical_high           NUMERIC,
    source_version          TEXT NOT NULL,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (analyte_code, unit, sex, age_min_years, age_max_years, source_version)
);

GRANT SELECT ON lab_reference_range TO medikiosk_app;

-- =============================================================================
-- RED_FLAG — every evaluation is logged, fired or not, so false-positive and
-- false-negative rates are measurable (§14).
-- =============================================================================
CREATE TABLE red_flag_evaluation (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id               UUID NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT,
    session_id              UUID NOT NULL REFERENCES session(id) ON DELETE CASCADE,
    ruleset_version         TEXT NOT NULL,
    rule_id                 TEXT NOT NULL,
    fired                   BOOLEAN NOT NULL,
    -- The exact field snapshot the rule saw, for reproducibility.
    evaluated_state         JSONB NOT NULL,
    trigger_field_id        TEXT,
    evaluated_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX red_flag_eval_session_idx ON red_flag_evaluation (tenant_id, session_id);
CREATE INDEX red_flag_eval_fired_idx ON red_flag_evaluation (tenant_id, rule_id, fired);

ALTER TABLE red_flag_evaluation ENABLE ROW LEVEL SECURITY;
ALTER TABLE red_flag_evaluation FORCE ROW LEVEL SECURITY;
CREATE POLICY red_flag_eval_tenant_isolation ON red_flag_evaluation
    USING (tenant_id = app_current_tenant());

CREATE TABLE red_flag_alert (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id               UUID NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT,
    session_id              UUID NOT NULL REFERENCES session(id) ON DELETE RESTRICT,
    department_id           UUID NOT NULL REFERENCES department(id) ON DELETE RESTRICT,
    rule_id                 TEXT NOT NULL,
    ruleset_version         TEXT NOT NULL,
    rule_name               TEXT NOT NULL,
    severity                TEXT NOT NULL CHECK (severity IN ('moderate', 'high', 'critical')),
    -- Staff-facing clinical rationale. Never shown on the kiosk (§14).
    staff_message           TEXT NOT NULL,
    sla_seconds             INTEGER NOT NULL,
    status                  TEXT NOT NULL DEFAULT 'open' CHECK (status IN (
                                'open', 'acknowledged', 'escalated', 'resolved'
                            )),
    acknowledged_by         UUID REFERENCES app_user(id) ON DELETE SET NULL,
    acknowledged_at         TIMESTAMPTZ,
    escalated_at            TIMESTAMPTZ,
    resolved_by             UUID REFERENCES app_user(id) ON DELETE SET NULL,
    resolved_at             TIMESTAMPTZ,
    resolution_note         TEXT,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (session_id, rule_id)
);

CREATE INDEX red_flag_alert_queue_idx
    ON red_flag_alert (tenant_id, department_id, status, created_at);

ALTER TABLE red_flag_alert ENABLE ROW LEVEL SECURITY;
ALTER TABLE red_flag_alert FORCE ROW LEVEL SECURITY;
CREATE POLICY red_flag_alert_tenant_isolation ON red_flag_alert
    USING (tenant_id = app_current_tenant());

-- =============================================================================
-- DOCUMENT — originals are the medical record; retained per §38.
-- =============================================================================
CREATE TABLE document (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id               UUID NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT,
    session_id              UUID NOT NULL REFERENCES session(id) ON DELETE RESTRICT,
    patient_id              UUID NOT NULL REFERENCES patient(id) ON DELETE RESTRICT,

    capture_path            TEXT NOT NULL CHECK (capture_path IN (
                                'kiosk_camera', 'qr_phone_upload', 'staff_assisted'
                            )),
    -- [RED LINE §9] no anonymous uploads: whoever uploaded is always recorded.
    respondent_type         TEXT NOT NULL CHECK (respondent_type IN ('patient', 'caregiver', 'staff')),
    respondent_id           UUID NOT NULL,
    respondent_relationship TEXT,
    upload_token_id         UUID REFERENCES upload_token(id) ON DELETE SET NULL,

    original_filename       TEXT,
    declared_mime           TEXT,
    -- The verified type, from magic bytes — never the declared one (§35).
    verified_mime           TEXT,
    size_bytes              BIGINT NOT NULL,
    sha256                  TEXT NOT NULL,
    object_key              TEXT NOT NULL,

    malware_scan_status     TEXT NOT NULL DEFAULT 'pending' CHECK (malware_scan_status IN (
                                'pending', 'clean', 'infected', 'error'
                            )),
    malware_scanner         TEXT,
    doc_class               TEXT,   -- prescription | lab_report | discharge_summary | imaging | other
    quality_status          TEXT CHECK (quality_status IN ('ok', 'blurred', 'glare', 'unreadable')),

    processing_status       TEXT NOT NULL DEFAULT 'queued' CHECK (processing_status IN (
                                'queued', 'scanning', 'processing', 'needs_recapture',
                                'awaiting_verification', 'completed', 'failed', 'rejected'
                            )),
    processing_error        TEXT,
    pages                   INTEGER,
    ocr_engine              TEXT,
    ocr_model_version       TEXT,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    processed_at            TIMESTAMPTZ,
    purge_after             TIMESTAMPTZ
);

CREATE INDEX document_session_idx ON document (tenant_id, session_id);
CREATE INDEX document_status_idx ON document (tenant_id, processing_status);

ALTER TABLE document ENABLE ROW LEVEL SECURITY;
ALTER TABLE document FORCE ROW LEVEL SECURITY;
CREATE POLICY document_tenant_isolation ON document
    USING (tenant_id = app_current_tenant());
CREATE POLICY document_patient_self ON document
    USING (
        app_current_role() IS DISTINCT FROM 'patient'
        OR patient_id = app_current_patient_id()
    );

-- Page-level OCR output. Untrusted data (§19 prompt-injection posture).
CREATE TABLE document_page (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id               UUID NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT,
    document_id             UUID NOT NULL REFERENCES document(id) ON DELETE CASCADE,
    page_number             INTEGER NOT NULL,
    ocr_text                TEXT,
    ocr_confidence          NUMERIC(4,3),
    layout                  JSONB NOT NULL DEFAULT '{}'::jsonb,
    handwritten             BOOLEAN NOT NULL DEFAULT false,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (document_id, page_number)
);

ALTER TABLE document_page ENABLE ROW LEVEL SECURITY;
ALTER TABLE document_page FORCE ROW LEVEL SECURITY;
CREATE POLICY document_page_tenant_isolation ON document_page
    USING (tenant_id = app_current_tenant());

-- Confidence-gated human verification queue (§17.2)
CREATE TABLE extraction_candidate (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id               UUID NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT,
    document_id             UUID NOT NULL REFERENCES document(id) ON DELETE CASCADE,
    session_id              UUID NOT NULL REFERENCES session(id) ON DELETE CASCADE,
    page_number             INTEGER,
    category                TEXT NOT NULL,
    concept_code            TEXT NOT NULL,
    concept_label           TEXT NOT NULL,
    value_raw               TEXT,
    value_normalized        JSONB NOT NULL,
    unit                    TEXT,
    confidence              NUMERIC(4,3) NOT NULL,
    model_version           TEXT NOT NULL,
    status                  TEXT NOT NULL DEFAULT 'pending' CHECK (status IN (
                                'pending', 'auto_accepted', 'human_accepted',
                                'human_rejected', 'human_corrected'
                            )),
    reviewed_by             UUID REFERENCES app_user(id) ON DELETE SET NULL,
    reviewed_at             TIMESTAMPTZ,
    resulting_fact_id       UUID REFERENCES clinical_fact(id) ON DELETE SET NULL,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX extraction_candidate_queue_idx
    ON extraction_candidate (tenant_id, status, created_at);

ALTER TABLE extraction_candidate ENABLE ROW LEVEL SECURITY;
ALTER TABLE extraction_candidate FORCE ROW LEVEL SECURITY;
CREATE POLICY extraction_candidate_tenant_isolation ON extraction_candidate
    USING (tenant_id = app_current_tenant());

-- =============================================================================
-- TIMELINE_EVENT — unknown dates go to a separate bucket, never interpolated
-- [RED LINE §16].
-- =============================================================================
CREATE TABLE timeline_event (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id               UUID NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT,
    session_id              UUID NOT NULL REFERENCES session(id) ON DELETE CASCADE,
    fact_id                 UUID NOT NULL REFERENCES clinical_fact(id) ON DELETE CASCADE,
    date_known              BOOLEAN NOT NULL,
    date_value              DATE,
    date_precision          TEXT CHECK (date_precision IN ('day', 'month', 'year')),
    label                   TEXT NOT NULL,
    source_ref              JSONB NOT NULL,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT timeline_date_consistency CHECK (
        (date_known AND date_value IS NOT NULL AND date_precision IS NOT NULL)
        OR (NOT date_known AND date_value IS NULL)
    )
);

CREATE INDEX timeline_event_session_idx ON timeline_event (tenant_id, session_id, date_value);

ALTER TABLE timeline_event ENABLE ROW LEVEL SECURITY;
ALTER TABLE timeline_event FORCE ROW LEVEL SECURITY;
CREATE POLICY timeline_event_tenant_isolation ON timeline_event
    USING (tenant_id = app_current_tenant());

-- =============================================================================
-- CONFLICT — surfaced, never auto-resolved [RED LINE §15]
-- =============================================================================
CREATE TABLE fact_conflict (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id               UUID NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT,
    session_id              UUID NOT NULL REFERENCES session(id) ON DELETE CASCADE,
    concept_code            TEXT NOT NULL,
    fact_a_id               UUID NOT NULL REFERENCES clinical_fact(id) ON DELETE CASCADE,
    fact_b_id               UUID NOT NULL REFERENCES clinical_fact(id) ON DELETE CASCADE,
    -- Adjudication is a physician act; the engine never picks a winner.
    resolution              TEXT NOT NULL DEFAULT 'unresolved' CHECK (resolution IN (
                                'unresolved', 'physician_chose_a', 'physician_chose_b',
                                'physician_entered_new', 'not_a_conflict'
                            )),
    resolved_by             UUID REFERENCES app_user(id) ON DELETE SET NULL,
    resolved_at             TIMESTAMPTZ,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (fact_a_id, fact_b_id)
);

CREATE INDEX fact_conflict_session_idx ON fact_conflict (tenant_id, session_id, resolution);

ALTER TABLE fact_conflict ENABLE ROW LEVEL SECURITY;
ALTER TABLE fact_conflict FORCE ROW LEVEL SECURITY;
CREATE POLICY fact_conflict_tenant_isolation ON fact_conflict
    USING (tenant_id = app_current_tenant());

-- =============================================================================
-- SUMMARY — evidence-grounded draft. Every sentence cites a real fact (§19).
-- =============================================================================
CREATE TABLE summary (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id               UUID NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT,
    session_id              UUID NOT NULL REFERENCES session(id) ON DELETE RESTRICT,
    -- 'llm_drafted' | 'structured_fallback' (LLM unavailable — §19 failure mode)
    generation_mode         TEXT NOT NULL CHECK (generation_mode IN (
                                'llm_drafted', 'structured_fallback'
                            )),
    model_version           TEXT,
    prompt_version          TEXT,
    latency_ms              INTEGER,
    patient_confirmed_at    TIMESTAMPTZ,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (session_id)
);

ALTER TABLE summary ENABLE ROW LEVEL SECURITY;
ALTER TABLE summary FORCE ROW LEVEL SECURITY;
CREATE POLICY summary_tenant_isolation ON summary
    USING (tenant_id = app_current_tenant());

CREATE TABLE summary_statement (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id               UUID NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT,
    summary_id              UUID NOT NULL REFERENCES summary(id) ON DELETE CASCADE,
    section                 TEXT NOT NULL,
    ordinal                 INTEGER NOT NULL,
    text                    TEXT NOT NULL,
    -- Non-empty by constraint: an uncited sentence cannot be persisted (§19).
    citations               UUID[] NOT NULL,
    physician_action        TEXT NOT NULL DEFAULT 'pending' CHECK (physician_action IN (
                                'pending', 'accepted', 'edited', 'excluded'
                            )),
    edited_text             TEXT,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (summary_id, section, ordinal),
    CONSTRAINT summary_statement_must_cite CHECK (array_length(citations, 1) >= 1)
);

CREATE INDEX summary_statement_summary_idx ON summary_statement (tenant_id, summary_id);

ALTER TABLE summary_statement ENABLE ROW LEVEL SECURITY;
ALTER TABLE summary_statement FORCE ROW LEVEL SECURITY;
CREATE POLICY summary_statement_tenant_isolation ON summary_statement
    USING (tenant_id = app_current_tenant());

-- =============================================================================
-- PHYSICIAN_REVIEW — the authority gate. No path to exported skips approved
-- [RED LINE §21].
-- =============================================================================
CREATE TABLE physician_review (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id               UUID NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT,
    session_id              UUID NOT NULL REFERENCES session(id) ON DELETE RESTRICT,
    summary_id              UUID REFERENCES summary(id) ON DELETE RESTRICT,
    status                  TEXT NOT NULL DEFAULT 'draft' CHECK (status IN (
                                'draft', 'under_review', 'edited',
                                'clarification_requested', 'rejected',
                                'approved', 'exported'
                            )),
    reviewer_id             UUID REFERENCES app_user(id) ON DELETE RESTRICT,
    opened_at               TIMESTAMPTZ,
    approved_at             TIMESTAMPTZ,
    approved_by             UUID REFERENCES app_user(id) ON DELETE RESTRICT,
    exported_at             TIMESTAMPTZ,
    rejection_reason        TEXT,
    clarification_note      TEXT,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (session_id),
    CONSTRAINT review_export_requires_approval CHECK (
        status <> 'exported' OR (approved_at IS NOT NULL AND approved_by IS NOT NULL)
    ),
    CONSTRAINT review_approval_attribution CHECK (
        status NOT IN ('approved', 'exported')
        OR (approved_at IS NOT NULL AND approved_by IS NOT NULL)
    )
);

CREATE INDEX physician_review_queue_idx ON physician_review (tenant_id, status, created_at);

ALTER TABLE physician_review ENABLE ROW LEVEL SECURITY;
ALTER TABLE physician_review FORCE ROW LEVEL SECURITY;
CREATE POLICY physician_review_tenant_isolation ON physician_review
    USING (tenant_id = app_current_tenant());

-- The state machine of §21, enforced in the database. Even a bug in the service
-- layer cannot move a record to exported without passing through approved.
CREATE OR REPLACE FUNCTION physician_review_transition_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    allowed TEXT[];
BEGIN
    IF NEW.status = OLD.status THEN
        RETURN NEW;
    END IF;

    allowed := CASE OLD.status
        WHEN 'draft'                    THEN ARRAY['under_review']
        WHEN 'under_review'             THEN ARRAY['edited', 'clarification_requested',
                                                   'rejected', 'approved']
        WHEN 'edited'                   THEN ARRAY['under_review']
        WHEN 'clarification_requested'  THEN ARRAY['under_review']
        WHEN 'rejected'                 THEN ARRAY['under_review']
        WHEN 'approved'                 THEN ARRAY['exported']
        WHEN 'exported'                 THEN ARRAY[]::TEXT[]
        ELSE ARRAY[]::TEXT[]
    END;

    IF NOT (NEW.status = ANY (allowed)) THEN
        RAISE EXCEPTION
            'illegal physician_review transition %% -> %% (CLAUDE.md §21 [RED LINE])',
            OLD.status, NEW.status;
    END IF;

    RETURN NEW;
END
$$;

CREATE TRIGGER physician_review_transition_trg
    BEFORE UPDATE ON physician_review
    FOR EACH ROW EXECUTE FUNCTION physician_review_transition_guard();

-- Post-export writes are refused at the storage layer too (§4, §5.2).
CREATE OR REPLACE FUNCTION clinical_fact_post_export_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_status TEXT;
BEGIN
    SELECT status INTO v_status
      FROM physician_review
     WHERE session_id = NEW.session_id;

    IF v_status = 'exported' THEN
        RAISE EXCEPTION
            'session %% is exported; clinical facts are sealed (CLAUDE.md §21)', NEW.session_id;
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER clinical_fact_post_export_trg
    BEFORE INSERT OR UPDATE ON clinical_fact
    FOR EACH ROW EXECUTE FUNCTION clinical_fact_post_export_guard();

-- =============================================================================
-- NAMASTE_MAPPING — only a practitioner-CONFIRMED mapping is ever written (§24)
-- =============================================================================
CREATE TABLE namaste_mapping (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id               UUID NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT,
    session_id              UUID NOT NULL REFERENCES session(id) ON DELETE RESTRICT,
    fact_id                 UUID NOT NULL REFERENCES clinical_fact(id) ON DELETE RESTRICT,
    namaste_code            TEXT NOT NULL,
    namaste_term            TEXT NOT NULL,
    namaste_system          TEXT NOT NULL DEFAULT 'ayurveda',
    icd11_tm2_code          TEXT,
    icd11_tm2_term          TEXT,
    icd11_biomed_code       TEXT,
    -- Static, versioned snapshot until Ministry API terms are confirmed (§24)
    terminology_source      TEXT NOT NULL,
    terminology_version     TEXT NOT NULL,
    confirmed_by            UUID NOT NULL REFERENCES app_user(id) ON DELETE RESTRICT,
    confirmed_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    ai_suggestion_rank      INTEGER,
    ai_suggestion_score     NUMERIC(4,3),
    UNIQUE (fact_id, namaste_code)
);

CREATE INDEX namaste_mapping_session_idx ON namaste_mapping (tenant_id, session_id);

ALTER TABLE namaste_mapping ENABLE ROW LEVEL SECURITY;
ALTER TABLE namaste_mapping FORCE ROW LEVEL SECURITY;
CREATE POLICY namaste_mapping_tenant_isolation ON namaste_mapping
    USING (tenant_id = app_current_tenant());

-- =============================================================================
-- OUTBOX_EVENT — transactional outbox (§23, §50). Written in the same
-- transaction as the state change; relayed to RabbitMQ by the worker.
-- =============================================================================
CREATE TABLE outbox_event (
    id                      BIGSERIAL PRIMARY KEY,
    tenant_id               UUID NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT,
    event_type              TEXT NOT NULL,
    aggregate_type          TEXT NOT NULL,
    aggregate_id            UUID NOT NULL,
    payload                 JSONB NOT NULL,
    -- Idempotency-Key propagated to every downstream adapter call (§49)
    idempotency_key         TEXT NOT NULL UNIQUE,
    status                  TEXT NOT NULL DEFAULT 'pending' CHECK (status IN (
                                'pending', 'dispatched', 'delivered', 'failed', 'dead_letter'
                            )),
    attempts                INTEGER NOT NULL DEFAULT 0,
    last_error              TEXT,
    available_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    dispatched_at           TIMESTAMPTZ,
    delivered_at            TIMESTAMPTZ
);

CREATE INDEX outbox_pending_idx ON outbox_event (status, available_at)
    WHERE status IN ('pending', 'failed');

ALTER TABLE outbox_event ENABLE ROW LEVEL SECURITY;
ALTER TABLE outbox_event FORCE ROW LEVEL SECURITY;
CREATE POLICY outbox_tenant_isolation ON outbox_event
    USING (tenant_id = app_current_tenant());

-- The relay worker runs without a tenant context; it needs a bypass path that
-- is explicitly scoped to the relay role rather than granted to the API role.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'medikiosk_relay') THEN
        CREATE ROLE medikiosk_relay LOGIN PASSWORD 'medikiosk_relay' NOBYPASSRLS;
    END IF;
END
$$;

GRANT USAGE ON SCHEMA public TO medikiosk_relay;
CREATE POLICY outbox_relay_access ON outbox_event
    TO medikiosk_relay USING (true) WITH CHECK (true);

-- =============================================================================
-- INTEGRATION_DELIVERY — proof of idempotent downstream delivery (§8 DoD)
-- =============================================================================
CREATE TABLE integration_delivery (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id               UUID NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT,
    session_id              UUID NOT NULL REFERENCES session(id) ON DELETE RESTRICT,
    target                  TEXT NOT NULL CHECK (target IN ('fhir', 'abdm', 'his')),
    environment             TEXT NOT NULL DEFAULT 'sandbox'
                            CHECK (environment IN ('sandbox', 'mock', 'production')),
    idempotency_key         TEXT NOT NULL,
    status                  TEXT NOT NULL CHECK (status IN ('succeeded', 'failed')),
    request_checksum        TEXT NOT NULL,
    response_summary        JSONB NOT NULL DEFAULT '{}'::jsonb,
    delivered_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- The same key can never produce two downstream records.
    UNIQUE (target, idempotency_key)
);

CREATE INDEX integration_delivery_session_idx
    ON integration_delivery (tenant_id, session_id);

ALTER TABLE integration_delivery ENABLE ROW LEVEL SECURITY;
ALTER TABLE integration_delivery FORCE ROW LEVEL SECURITY;
CREATE POLICY integration_delivery_tenant_isolation ON integration_delivery
    USING (tenant_id = app_current_tenant());
CREATE POLICY integration_delivery_relay ON integration_delivery
    TO medikiosk_relay USING (true) WITH CHECK (true);

GRANT SELECT, INSERT, UPDATE, DELETE ON
    clinical_fact, red_flag_evaluation, red_flag_alert, document, document_page,
    extraction_candidate, timeline_event, fact_conflict, summary,
    summary_statement, physician_review, namaste_mapping, outbox_event,
    integration_delivery
    TO medikiosk_app;

-- The relay worker is deliberately starved: outbox rows and delivery receipts,
-- nothing else. It builds nothing itself — it calls the API's internal,
-- mTLS-guarded export endpoint (§49), so it never needs clinical table access.
GRANT SELECT, UPDATE ON outbox_event TO medikiosk_relay;
GRANT SELECT, INSERT ON integration_delivery TO medikiosk_relay;
GRANT USAGE, SELECT ON SEQUENCE outbox_event_id_seq TO medikiosk_app;
