-- =============================================================================
-- MediKiosk migration 0004 — Governed reference data
--
-- This migration seeds data that is NOT patient data and NOT tenant
-- configuration: the governance registry of approved protocol versions, and the
-- lab reference ranges that make abnormality classification deterministic (§15).
--
-- It contains NO patient records and NO tenant records. Synthetic demo data is a
-- separate, explicitly-labelled script (scripts/seed_demo.py), because §28 makes
-- dev/staging synthetic-only and a migration that silently created "sample
-- patients" would blur that line.
-- =============================================================================

-- -----------------------------------------------------------------------------
-- Approved protocol versions (§10).
--
-- content_checksum is filled by the governance registration step, not hardcoded
-- here: whoever registers a version is asserting they reviewed THAT content.
-- A NULL checksum means "version approved, checksum not yet pinned", and
-- resolve_protocol() treats a pinned mismatch as fatal.
-- -----------------------------------------------------------------------------
INSERT INTO protocol_version
    (protocol_family, version, display_name, content_checksum, status,
     governance_reviewer, approved_at)
VALUES
    ('general_medicine', 'v1', 'General Medicine Intake v1',
     'PENDING_GOVERNANCE_PIN', 'active',
     'Clinical Governance Board (prototype ratification pending)', now()),
    ('ayush_ayurveda', 'v1', 'Ayurveda Intake v1 (Dashavidha Pariksha)',
     'PENDING_GOVERNANCE_PIN', 'active',
     'AYUSH Clinical Governance reviewers (prototype ratification pending)', now())
ON CONFLICT (protocol_family, version) DO NOTHING;

-- The sentinel is deliberately not a valid sha256, so a deployment that means to
-- pin the checksum can tell the difference between "pinned" and "not pinned".
-- resolve_protocol() only enforces equality when the stored value looks like a
-- real digest.
CREATE OR REPLACE FUNCTION protocol_checksum_is_pinned(p_checksum TEXT)
RETURNS BOOLEAN LANGUAGE sql IMMUTABLE AS $$
    SELECT p_checksum ~ '^[0-9a-f]{64}$'
$$;

GRANT EXECUTE ON FUNCTION protocol_checksum_is_pinned(TEXT) TO medikiosk_app;

-- -----------------------------------------------------------------------------
-- Lab reference ranges (§15).
--
-- The ONLY source of abnormality classification. AI's role in a lab value is
-- extracting the number and unit; the high/low/critical decision is this table
-- plus a comparison, and nothing else.
--
-- Values follow commonly-published adult reference intervals for Indian
-- laboratory practice. They are a STARTING POINT for the Clinical Governance
-- Board to ratify per-laboratory: reference intervals are assay-dependent, and a
-- production deployment must load the pilot laboratory's own ranges.
-- -----------------------------------------------------------------------------
INSERT INTO lab_reference_range
    (analyte_code, analyte_label, unit, sex, age_min_years, age_max_years,
     low, high, critical_low, critical_high, source_version)
VALUES
    -- Haematology
    ('hemoglobin', 'Haemoglobin', 'g/dL', 'male',   15, 200, 13.0, 17.0, 7.0,  20.0, 'ref-v1'),
    ('hemoglobin', 'Haemoglobin', 'g/dL', 'female', 15, 200, 12.0, 15.0, 7.0,  20.0, 'ref-v1'),
    ('hemoglobin', 'Haemoglobin', 'g/dL', 'any',     1,  14, 11.0, 14.0, 7.0,  20.0, 'ref-v1'),
    ('wbc',        'Total leucocyte count', '10^3/uL', 'any', 15, 200, 4.0, 11.0, 1.0, 30.0, 'ref-v1'),
    ('platelets',  'Platelet count', '10^3/uL', 'any', 15, 200, 150.0, 410.0, 20.0, 1000.0, 'ref-v1'),

    -- Glycaemic
    ('glucose_fasting', 'Fasting plasma glucose', 'mg/dL', 'any', 15, 200,
     70.0, 100.0, 50.0, 400.0, 'ref-v1'),
    ('glucose_random',  'Random plasma glucose',  'mg/dL', 'any', 15, 200,
     70.0, 140.0, 50.0, 450.0, 'ref-v1'),
    ('hba1c', 'HbA1c', '%', 'any', 15, 200, 4.0, 5.7, NULL, 14.0, 'ref-v1'),

    -- Renal
    ('creatinine', 'Serum creatinine', 'mg/dL', 'male',   15, 200, 0.7, 1.3, NULL, 5.0, 'ref-v1'),
    ('creatinine', 'Serum creatinine', 'mg/dL', 'female', 15, 200, 0.6, 1.1, NULL, 5.0, 'ref-v1'),
    ('urea',       'Blood urea',       'mg/dL', 'any',    15, 200, 15.0, 40.0, NULL, 150.0, 'ref-v1'),
    ('potassium',  'Serum potassium',  'mEq/L', 'any',    15, 200, 3.5, 5.1, 2.5, 6.5, 'ref-v1'),
    ('sodium',     'Serum sodium',     'mEq/L', 'any',    15, 200, 135.0, 145.0, 120.0, 160.0, 'ref-v1'),

    -- Hepatic
    ('bilirubin_total', 'Total bilirubin', 'mg/dL', 'any', 15, 200, 0.2, 1.2, NULL, 15.0, 'ref-v1'),
    ('alt', 'ALT (SGPT)', 'U/L', 'any', 15, 200, 7.0, 55.0, NULL, 1000.0, 'ref-v1'),
    ('ast', 'AST (SGOT)', 'U/L', 'any', 15, 200, 8.0, 48.0, NULL, 1000.0, 'ref-v1'),

    -- Lipids
    ('cholesterol_total', 'Total cholesterol', 'mg/dL', 'any', 15, 200,
     NULL, 200.0, NULL, NULL, 'ref-v1'),
    ('ldl', 'LDL cholesterol', 'mg/dL', 'any', 15, 200, NULL, 100.0, NULL, NULL, 'ref-v1'),
    ('hdl', 'HDL cholesterol', 'mg/dL', 'male',   15, 200, 40.0, NULL, NULL, NULL, 'ref-v1'),
    ('hdl', 'HDL cholesterol', 'mg/dL', 'female', 15, 200, 50.0, NULL, NULL, NULL, 'ref-v1'),
    ('triglycerides', 'Triglycerides', 'mg/dL', 'any', 15, 200,
     NULL, 150.0, NULL, 1000.0, 'ref-v1'),

    -- Thyroid
    ('tsh', 'TSH', 'mIU/L', 'any', 15, 200, 0.4, 4.0, NULL, 100.0, 'ref-v1'),

    -- Cardiac / inflammatory
    ('troponin_i', 'Troponin I', 'ng/mL', 'any', 15, 200, NULL, 0.04, NULL, 0.4, 'ref-v1'),
    ('crp', 'C-reactive protein', 'mg/L', 'any', 15, 200, NULL, 5.0, NULL, 100.0, 'ref-v1'),

    -- Vitals recorded as investigation values
    ('spo2', 'Oxygen saturation', '%', 'any', 0, 200, 94.0, 100.0, 88.0, NULL, 'ref-v1'),
    ('systolic_bp', 'Systolic blood pressure', 'mmHg', 'any', 15, 200,
     90.0, 140.0, 80.0, 200.0, 'ref-v1'),
    ('diastolic_bp', 'Diastolic blood pressure', 'mmHg', 'any', 15, 200,
     60.0, 90.0, 50.0, 130.0, 'ref-v1'),
    ('pulse', 'Pulse rate', 'bpm', 'any', 15, 200, 60.0, 100.0, 40.0, 150.0, 'ref-v1'),
    ('respiratory_rate', 'Respiratory rate', '/min', 'any', 15, 200,
     12.0, 20.0, 8.0, 30.0, 'ref-v1'),
    ('temperature', 'Body temperature', 'C', 'any', 0, 200, 36.1, 37.5, 35.0, 40.0, 'ref-v1')
ON CONFLICT (analyte_code, unit, sex, age_min_years, age_max_years, source_version)
DO NOTHING;
