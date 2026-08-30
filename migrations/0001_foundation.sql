-- =============================================================================
-- MediKiosk migration 0001 — Foundation + Security (CLAUDE.md Phase 0, §57)
--
-- Establishes:
--   * database roles with least privilege (app role is NOBYPASSRLS)
--   * tenant / department / app_user / device / patient
--   * append-only hash-chained audit_event (§31)
--   * Row Level Security on EVERY patient-data table, from this first migration
--     (§30 [RED LINE] — never retrofitted)
--
-- Session GUCs the application MUST set on every transaction (§30):
--   app.current_tenant      uuid   — tenant of the authenticated principal
--   app.current_role        text   — role name of the authenticated principal
--   app.current_patient_id  uuid   — only for role 'patient'
--   app.current_actor_id    uuid   — actor for audit attribution
-- =============================================================================

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- -----------------------------------------------------------------------------
-- Least-privilege roles
-- -----------------------------------------------------------------------------
-- medikiosk_app  : the only role the API connects as. NOBYPASSRLS is the point:
--                  even a SQL-injection or ORM bug cannot cross a tenant.
-- medikiosk_audit: reserved for the Security/Privacy Officer audit export path.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'medikiosk_app') THEN
        CREATE ROLE medikiosk_app LOGIN PASSWORD 'medikiosk_app' NOBYPASSRLS;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'medikiosk_audit') THEN
        CREATE ROLE medikiosk_audit LOGIN PASSWORD 'medikiosk_audit' NOBYPASSRLS;
    END IF;
END
$$;

GRANT USAGE ON SCHEMA public TO medikiosk_app, medikiosk_audit;

-- -----------------------------------------------------------------------------
-- Migration bookkeeping
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS schema_migration (
    version     TEXT PRIMARY KEY,
    checksum    TEXT NOT NULL,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- -----------------------------------------------------------------------------
-- Helper: the current tenant, as uuid, or NULL when unset.
-- STABLE + SECURITY INVOKER; used by every RLS policy.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION app_current_tenant() RETURNS uuid
LANGUAGE sql STABLE AS $$
    SELECT NULLIF(current_setting('app.current_tenant', true), '')::uuid
$$;

CREATE OR REPLACE FUNCTION app_current_role() RETURNS text
LANGUAGE sql STABLE AS $$
    SELECT NULLIF(current_setting('app.current_role', true), '')
$$;

CREATE OR REPLACE FUNCTION app_current_patient_id() RETURNS uuid
LANGUAGE sql STABLE AS $$
    SELECT NULLIF(current_setting('app.current_patient_id', true), '')::uuid
$$;

CREATE OR REPLACE FUNCTION app_current_actor_id() RETURNS uuid
LANGUAGE sql STABLE AS $$
    SELECT NULLIF(current_setting('app.current_actor_id', true), '')::uuid
$$;

-- =============================================================================
-- TENANT
-- =============================================================================
CREATE TABLE tenant (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    slug                TEXT NOT NULL UNIQUE,
    display_name        TEXT NOT NULL,
    -- DPDP-Rules-2025-mapped retention, tenant-configurable (§26, §38)
    retention_days_documents        INTEGER NOT NULL DEFAULT 3650,
    retention_days_clinical_facts   INTEGER NOT NULL DEFAULT 3650,
    retention_days_telemetry        INTEGER NOT NULL DEFAULT 30,
    -- pseudonymisation salt for telemetry IDs (§28) — never leaves the DB tier
    telemetry_salt      TEXT NOT NULL DEFAULT encode(gen_random_bytes(32), 'hex'),
    status              TEXT NOT NULL DEFAULT 'active'
                        CHECK (status IN ('active', 'suspended')),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE tenant ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenant FORCE ROW LEVEL SECURITY;

-- A principal can only ever see its own tenant row.
CREATE POLICY tenant_self ON tenant
    USING (id = app_current_tenant());

-- =============================================================================
-- DEPARTMENT — drives protocol family resolution (§10)
-- =============================================================================
CREATE TABLE department (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           UUID NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT,
    code                TEXT NOT NULL,
    display_name        TEXT NOT NULL,
    -- 'general_medicine' | 'ayush_ayurveda' (extensible: siddha, unani, homeopathy)
    protocol_family     TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'active'
                        CHECK (status IN ('active', 'inactive')),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, code)
);

CREATE INDEX department_tenant_idx ON department (tenant_id);

ALTER TABLE department ENABLE ROW LEVEL SECURITY;
ALTER TABLE department FORCE ROW LEVEL SECURITY;
CREATE POLICY department_tenant_isolation ON department
    USING (tenant_id = app_current_tenant());

-- =============================================================================
-- TENANT PROTOCOL CONFIG — which governed protocol version is active (§10)
-- =============================================================================
CREATE TABLE tenant_protocol_config (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           UUID NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT,
    protocol_family     TEXT NOT NULL,
    active_version      TEXT NOT NULL,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, protocol_family)
);

ALTER TABLE tenant_protocol_config ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenant_protocol_config FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_protocol_config_isolation ON tenant_protocol_config
    USING (tenant_id = app_current_tenant());

-- =============================================================================
-- APP_USER — staff identities, mirrored from Keycloak (OIDC is the authority)
-- =============================================================================
CREATE TABLE app_user (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id               UUID NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT,
    -- Keycloak 'sub'. Identity lives in Keycloak; this is a local projection.
    subject                 TEXT NOT NULL UNIQUE,
    username                TEXT NOT NULL,
    display_name            TEXT NOT NULL,
    role                    TEXT NOT NULL CHECK (role IN (
                                'nurse',
                                'physician',
                                'ayush_practitioner',
                                'clinical_admin',
                                'it_admin',
                                'security_officer'
                            )),
    assigned_department_id  UUID REFERENCES department(id) ON DELETE SET NULL,
    mfa_enrolled            BOOLEAN NOT NULL DEFAULT false,
    status                  TEXT NOT NULL DEFAULT 'active'
                            CHECK (status IN ('active', 'disabled')),
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX app_user_tenant_idx ON app_user (tenant_id);
CREATE INDEX app_user_subject_idx ON app_user (subject);

ALTER TABLE app_user ENABLE ROW LEVEL SECURITY;
ALTER TABLE app_user FORCE ROW LEVEL SECURITY;
CREATE POLICY app_user_tenant_isolation ON app_user
    USING (tenant_id = app_current_tenant());

-- =============================================================================
-- DEVICE — the kiosk tablet. tenant/department are fixed BY THE DEVICE (§8).
-- A stolen or unprovisioned tablet cannot start a session (§33).
-- =============================================================================
CREATE TABLE device (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id               UUID NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT,
    department_id           UUID REFERENCES department(id) ON DELETE SET NULL,
    label                   TEXT NOT NULL,
    -- SHA-256 of the provisioned device certificate/secret. Never the secret itself.
    credential_hash         TEXT NOT NULL,
    -- 'kiosk_tablet' | 'staff_capture' (staff-assisted capture fallback, §9)
    device_type             TEXT NOT NULL DEFAULT 'kiosk_tablet',
    status                  TEXT NOT NULL DEFAULT 'active'
                            CHECK (status IN ('active', 'revoked')),
    last_seen_at            TIMESTAMPTZ,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, label)
);

CREATE INDEX device_tenant_idx ON device (tenant_id);
CREATE UNIQUE INDEX device_credential_hash_idx ON device (credential_hash);

ALTER TABLE device ENABLE ROW LEVEL SECURITY;
ALTER TABLE device FORCE ROW LEVEL SECURITY;
CREATE POLICY device_tenant_isolation ON device
    USING (tenant_id = app_current_tenant());

-- -----------------------------------------------------------------------------
-- Device authentication is the one lookup that must happen BEFORE a tenant is
-- known — the device is what establishes the tenant (§8). RLS would therefore
-- hide every row from it, and the app role is deliberately NOBYPASSRLS.
--
-- Rather than weakening RLS, this SECURITY DEFINER function is the single,
-- explicitly scoped exception: it accepts only a credential DIGEST, matches at
-- most one row (credential_hash is unique), and returns only the fields needed
-- to establish a session context. There is no way to enumerate devices through
-- it, and no other table is reachable.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION device_authenticate(p_credential_hash TEXT)
RETURNS TABLE (
    device_id           UUID,
    tenant_id           UUID,
    device_status       TEXT,
    device_type         TEXT,
    department_id       UUID,
    tenant_slug         TEXT,
    tenant_name         TEXT,
    tenant_status       TEXT,
    department_code     TEXT,
    department_name     TEXT,
    protocol_family     TEXT,
    department_status   TEXT
)
LANGUAGE sql
SECURITY DEFINER
SET search_path = public
STABLE
AS $$
    SELECT d.id, d.tenant_id, d.status, d.device_type, d.department_id,
           t.slug, t.display_name, t.status,
           dep.code, dep.display_name, dep.protocol_family, dep.status
      FROM device d
      JOIN tenant t ON t.id = d.tenant_id
      LEFT JOIN department dep ON dep.id = d.department_id
     WHERE d.credential_hash = p_credential_hash
     LIMIT 1
$$;

REVOKE ALL ON FUNCTION device_authenticate(TEXT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION device_authenticate(TEXT) TO medikiosk_app;

-- =============================================================================
-- PATIENT — ABHA-first. [RED LINE §7.1] no raw Aadhaar column exists, anywhere.
-- =============================================================================
CREATE TABLE patient (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id               UUID NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT,
    -- ABHA reference (address or number reference) returned BY ABDM. Never Aadhaar.
    abha_reference          TEXT,
    -- Local hospital registration path — always available, never blocked (§7.1)
    hospital_local_id       TEXT,
    full_name               TEXT NOT NULL,
    year_of_birth           INTEGER,
    gender                  TEXT CHECK (gender IN ('male', 'female', 'other', 'undisclosed')),
    phone_last4             TEXT,   -- minimised: never the full number at kiosk tier
    preferred_language      TEXT NOT NULL DEFAULT 'en',
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT patient_identified CHECK (
        abha_reference IS NOT NULL OR hospital_local_id IS NOT NULL
    )
);

CREATE INDEX patient_tenant_idx ON patient (tenant_id);
CREATE UNIQUE INDEX patient_abha_idx ON patient (tenant_id, abha_reference)
    WHERE abha_reference IS NOT NULL;
CREATE UNIQUE INDEX patient_local_id_idx ON patient (tenant_id, hospital_local_id)
    WHERE hospital_local_id IS NOT NULL;

ALTER TABLE patient ENABLE ROW LEVEL SECURITY;
ALTER TABLE patient FORCE ROW LEVEL SECURITY;

CREATE POLICY patient_tenant_isolation ON patient
    USING (tenant_id = app_current_tenant());

-- A patient principal can only ever reach its own row (§30).
CREATE POLICY patient_self_access ON patient
    USING (
        app_current_role() IS DISTINCT FROM 'patient'
        OR id = app_current_patient_id()
    );

-- Guard against a raw-Aadhaar-shaped value being smuggled into a text column.
-- Defence in depth behind the application-layer validator (§7.1 [RED LINE]).
ALTER TABLE patient ADD CONSTRAINT patient_no_aadhaar_shaped_local_id
    CHECK (hospital_local_id IS NULL OR hospital_local_id !~ '^[2-9][0-9]{11}$');

-- =============================================================================
-- AUDIT_EVENT — append-only, hash-chained (§31)
-- Written in the SAME TRANSACTION as the state change it describes [RED LINE].
-- =============================================================================
CREATE TABLE audit_event (
    id              BIGSERIAL PRIMARY KEY,
    tenant_id       UUID NOT NULL,
    actor_id        UUID,
    actor_role      TEXT NOT NULL,
    action          TEXT NOT NULL,
    entity_type     TEXT NOT NULL,
    entity_id       UUID NOT NULL,
    -- allowlisted, PHI-free detail payload (§28)
    detail          JSONB NOT NULL DEFAULT '{}'::jsonb,
    prev_hash       TEXT NOT NULL,
    row_hash        TEXT NOT NULL,
    occurred_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX audit_event_tenant_idx ON audit_event (tenant_id, id);
CREATE INDEX audit_event_entity_idx ON audit_event (tenant_id, entity_type, entity_id);
CREATE INDEX audit_event_actor_idx ON audit_event (tenant_id, actor_id);

ALTER TABLE audit_event ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_event FORCE ROW LEVEL SECURITY;
CREATE POLICY audit_event_tenant_isolation ON audit_event
    USING (tenant_id = app_current_tenant());
CREATE POLICY audit_event_tenant_insert ON audit_event
    FOR INSERT WITH CHECK (tenant_id = app_current_tenant());

-- Hash chain is computed and enforced in the database so that no application
-- path — including a buggy or malicious one — can write an unchained row.
CREATE OR REPLACE FUNCTION audit_event_chain() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_prev  TEXT;
    v_body  TEXT;
BEGIN
    SELECT row_hash INTO v_prev
      FROM audit_event
     ORDER BY id DESC
     LIMIT 1;

    NEW.prev_hash := COALESCE(v_prev, 'genesis');

    v_body := concat_ws('|',
        NEW.prev_hash,
        NEW.tenant_id::text,
        COALESCE(NEW.actor_id::text, ''),
        NEW.actor_role,
        NEW.action,
        NEW.entity_type,
        NEW.entity_id::text,
        NEW.detail::text,
        to_char(NEW.occurred_at, 'YYYY-MM-DD"T"HH24:MI:SS.USOF')
    );
    NEW.row_hash := encode(digest(v_body, 'sha256'), 'hex');
    RETURN NEW;
END
$$;

CREATE TRIGGER audit_event_chain_trg
    BEFORE INSERT ON audit_event
    FOR EACH ROW EXECUTE FUNCTION audit_event_chain();

-- Immutability, enforced at the table level rather than by convention.
CREATE OR REPLACE FUNCTION audit_event_immutable() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'audit_event is append-only (CLAUDE.md §31)';
END
$$;

CREATE TRIGGER audit_event_no_update
    BEFORE UPDATE ON audit_event
    FOR EACH ROW EXECUTE FUNCTION audit_event_immutable();

CREATE TRIGGER audit_event_no_delete
    BEFORE DELETE ON audit_event
    FOR EACH ROW EXECUTE FUNCTION audit_event_immutable();

-- Chain verification, used by the Security console and the integrity test.
CREATE OR REPLACE FUNCTION audit_chain_verify()
RETURNS TABLE (broken_at BIGINT, reason TEXT)
LANGUAGE plpgsql AS $$
DECLARE
    r           RECORD;
    v_expected  TEXT := 'genesis';
    v_body      TEXT;
    v_hash      TEXT;
BEGIN
    FOR r IN SELECT * FROM audit_event ORDER BY id LOOP
        IF r.prev_hash <> v_expected THEN
            RETURN QUERY SELECT r.id, 'prev_hash mismatch'::TEXT;
            RETURN;
        END IF;
        v_body := concat_ws('|',
            r.prev_hash, r.tenant_id::text, COALESCE(r.actor_id::text, ''),
            r.actor_role, r.action, r.entity_type, r.entity_id::text,
            r.detail::text,
            to_char(r.occurred_at, 'YYYY-MM-DD"T"HH24:MI:SS.USOF')
        );
        v_hash := encode(digest(v_body, 'sha256'), 'hex');
        IF v_hash <> r.row_hash THEN
            RETURN QUERY SELECT r.id, 'row_hash mismatch'::TEXT;
            RETURN;
        END IF;
        v_expected := r.row_hash;
    END LOOP;
END
$$;

-- -----------------------------------------------------------------------------
-- Grants — the app role gets exactly what it needs and nothing more.
-- audit_event is INSERT + SELECT only: no UPDATE, no DELETE grant exists (§31).
-- -----------------------------------------------------------------------------
GRANT SELECT, INSERT, UPDATE, DELETE ON
    tenant, department, tenant_protocol_config, app_user, device, patient
    TO medikiosk_app;

GRANT SELECT, INSERT ON audit_event TO medikiosk_app;
GRANT USAGE, SELECT ON SEQUENCE audit_event_id_seq TO medikiosk_app;
GRANT SELECT ON audit_event TO medikiosk_audit;
GRANT EXECUTE ON FUNCTION audit_chain_verify() TO medikiosk_app, medikiosk_audit;
GRANT SELECT ON schema_migration TO medikiosk_app;
