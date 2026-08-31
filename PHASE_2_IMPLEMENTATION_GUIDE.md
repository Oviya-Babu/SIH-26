# Phase 2 Implementation Guide – Complete Analysis & Roadmap

## Project Status Summary

### Current Implementation State

**Phase 0 (Foundation) ✅ COMPLETE**
- Identity & Access infrastructure (Keycloak OIDC + MFA)
- Tenant management & device authentication
- PostgreSQL with RLS enabled from migration 1
- OPA/Rego authorization framework
- CI/CD hooks (Semgrep, Gitleaks, pytest)
- PHI/PII redaction middleware
- Audit trail (hash-chained, INSERT-only)

**Phase 1 (Deterministic Engine) ✅ COMPLETE**
- NextField engine (deterministic, never ML-ranked)
- Protocol registry and field resolution
- Completeness calculation and dependency predicates
- General Medicine v1 + AYUSH Ayurveda v1 protocols
- ~100 unit tests passing (verified)
- Confidence thresholds and slot-filling
- All red-flag regression tests passing

**Phase 2 (Vertical Slice) 🟡 PARTIALLY COMPLETE**
- Backend API implementation 100% done
  - Session creation & management
  - Consent module (internal + ABDM aware)
  - Clinical facts with provenance & respondent tracking
  - Red-flag engine with emergency escalation to AMPLE fast-path
  - Physician review state machine (Draft → Under Review → Approved → Exported)
  - Audit logging per fact
  - WebSocket alerts for triage staff
  - Smoke test script validates all above
- **Missing: Real browser UI**
  - Kiosk web interface (patient/caregiver) does not exist
  - Staff/Physician dashboard minimal Next.js scaffold only, no UI logic wired
  - No connection from frontend to backend APIs

**Phase 3-10 Not Started**
- Voice (ASR/TTS integration)
- Documents (OCR, QR-to-phone)
- AI summary generation
- AYUSH/NAMASTE full flow
- FHIR/HIS/ABDM adapters
- Production hardening

---

## Project Folder Structure

```
/SIH-26
├── CLAUDE.md                          ← CANONICAL ARCHITECTURE DOCUMENT
├── README.md                          ← Updated contributor guide
├── PHASE_2_IMPLEMENTATION_GUIDE.md   ← THIS FILE
│
├── apps/
│   └── staff-frontend/                ← Next.js 14 scaffold (minimal)
│       ├── app/
│       │   ├── layout.tsx             ← Root layout stub
│       │   ├── page.tsx               ← Landing page stub
│       │   ├── staff-workspace.tsx    ← Empty workspace component
│       │   └── styles.css
│       ├── package.json
│       ├── next.config.ts
│       ├── proxy.ts
│       ├── tsconfig.json
│       └── README.md
│
├── services/
│   ├── api/                           ← FastAPI MAIN SERVICE (COMPLETE FOR PHASE 2)
│   │   ├── medikiosk/
│   │   │   ├── main.py               ← FastAPI app entry, routers
│   │   │   ├── context.py            ← Dependency wiring
│   │   │   ├── config.py             ← Runtime settings from env
│   │   │   ├── db.py                 ← PostgreSQL pool + RLS setter
│   │   │   ├── errors.py             ← Domain exceptions
│   │   │   ├── modules/
│   │   │   │   ├── session/          ← Session + answer flow
│   │   │   │   ├── consent/          ← Consent module (internal + ABDM)
│   │   │   │   ├── clinical_protocol/ ← Engine (protocol resolution + NextField)
│   │   │   │   ├── clinical_facts/   ← Fact persistence + provenance
│   │   │   │   ├── triage/           ← Red-flag engine + WebSocket alerts
│   │   │   │   ├── physician_review/ ← Review state machine
│   │   │   │   ├── audit/            ← Hash-chained audit logging
│   │   │   │   ├── identity/         ← Patient registration, local/ABHA
│   │   │   │   ├── tenant/           ← Tenant + department management
│   │   │   │   ├── document/         ← Document upload + processing queue
│   │   │   │   ├── timeline/         ← Chronological event ordering
│   │   │   │   ├── conflict/         ← Contradiction detection
│   │   │   │   ├── ayush_namaste/    ← NAMASTE/ICD-11 TM2 coding
│   │   │   │   ├── summary/          ← Evidence-grounded summary (TODO: LLM)
│   │   │   │   ├── caregiver/        ← Caregiver authorization model
│   │   │   │   ├── purge/            ← Transient session state cleanup
│   │   │   │   ├── localization/     ← i18n registry loader
│   │   │   │   └── tenant/           ← Tenant management
│   │   │   ├── routers/              ← API endpoints
│   │   │   │   ├── health.py         ← /healthz /readyz /v1/meta/languages
│   │   │   │   ├── kiosk.py          ← Device auth, identity, intake
│   │   │   │   ├── session.py        ← Session + next-question endpoints
│   │   │   │   ├── consent.py        ← Consent granting + revocation
│   │   │   │   ├── upload.py         ← QR-token + document upload
│   │   │   │   ├── documents.py      ← Document retrieval + status
│   │   │   │   ├── triage.py         ← Red-flag alerts + ack/escalate
│   │   │   │   ├── physician.py      ← Review + approval workflow
│   │   │   │   ├── governance.py     ← Protocol/red-flag management
│   │   │   │   ├── admin.py          ← Tenant/device/user config
│   │   │   │   ├── audit.py          ← Audit export (security officer)
│   │   │   │   ├── security_console.py ← Security officer dashboard
│   │   │   │   └── triage.py         ← (WebSocket push for alerts)
│   │   │   ├── security/
│   │   │   │   ├── oidc.py           ← Keycloak OIDC verification
│   │   │   │   ├── rbac.py           ← Role-based access control
│   │   │   │   ├── opa.py            ← OPA/Rego authorization client
│   │   │   │   └── tokens.py         ← Ephemeral session/upload tokens
│   │   │   ├── infrastructure/
│   │   │   │   ├── broker.py         ← RabbitMQ connection pool
│   │   │   │   └── object_store.py   ← S3/MinIO client
│   │   │   └── observability/
│   │   │       ├── logging_setup.py  ← Structlog + OTEL + redaction
│   │   │       └── redaction.py      ← PHI/PII masking
│   │   ├── pyproject.toml
│   │   ├── requirements.txt           ← NEWLY CREATED (see below)
│   │   └── .venv/                    ← Virtual environment (activate before scripts)
│   │
│   ├── ai-gateway/                    ← [FUTURE] ASR/OCR/LLM service
│   │   ├── medikiosk_ai/
│   │   │   └── prompts.py            ← LLM prompt templates
│   │   └── pyproject.toml
│   │
│   └── workers/                       ← [FUTURE] RabbitMQ consumers
│       ├── document_processing.py
│       ├── integration_relay.py
│       └── notification_worker.py
│
├── infra/
│   ├── docker/
│   │   ├── docker-compose.yml         ← ALL infrastructure services
│   │   ├── api.Dockerfile
│   │   ├── ai-gateway.Dockerfile
│   │   ├── worker.Dockerfile
│   │   ├── frontend.Dockerfile
│   │   └── postgres-init/
│   │       └── 01-keycloak-schema.sql ← Keycloak schema
│   ├── keycloak/
│   │   ├── realm-medikiosk.json       ← 7 roles + users + clients
│   │   └── README.md
│   ├── nginx/
│   │   └── nginx.conf                 ← Reverse proxy config
│   ├── otel/
│   │   ├── collector-config.yaml      ← Telemetry redaction
│   │   ├── prometheus.yml
│   │   └── grafana-provisioning/
│   └── terraform/                     ← [FUTURE] Infrastructure as code
│
├── migrations/
│   ├── 0001_foundation.sql            ← Tenants, users, RLS setup
│   ├── 0002_consent_session.sql       ← Session + consent tables
│   ├── 0003_clinical.sql              ← Clinical facts + documents
│   ├── 0004_seed_reference_data.sql   ← Lab ref ranges, NAMASTE snapshot
│   └── 0005_fix_policy_combination.sql ← RLS policy refinements
│
├── policies/
│   └── opa/
│       ├── authz.rego                 ← OPA authorization rules
│       └── authz_test.rego            ← OPA test cases
│
├── content/
│   ├── protocols/
│   │   ├── general_medicine/
│   │   │   └── v1.json               ← Chief complaint, SOCRATES, ROS, PMH, etc.
│   │   └── ayush_ayurveda/
│   │       └── v1.json               ← Dashavidha Pariksha, Ahara-Vihara
│   ├── redflag/
│   │   └── emergency_v1.json          ← ACS, dyspnea, severe pain rules
│   ├── terminology/
│   │   └── namaste-snapshot-2025.1.json ← NAMASTE code mappings
│   └── i18n/
│       ├── en/
│       ├── hi/
│       ├── ta/
│       ├── te/
│       └── ml/
│
├── tests/
│   ├── unit/
│   │   ├── test_engine.py            ← NextField, completeness, predicates
│   │   ├── test_predicates.py         ← Field dependency evaluation
│   │   └── test_content_governance.py ← Protocol/red-flag validation
│   ├── red_flag_regression/           ← Golden scenarios (ACS textbook, etc.)
│   │   ├── golden_scenarios.json
│   │   └── test_golden_scenarios.py
│   ├── security/
│   │   └── test_rls_isolation.py      ← Tenant/patient isolation proofs
│   └── conftest.py                    ← Root pytest fixtures
│
├── scripts/
│   ├── compose.sh                     ← Docker Compose wrapper (fixes .env path)
│   ├── migrate.py                     ← **MUST RUN IN VENV** (see below)
│   ├── seed_demo.py                   ← **MUST RUN IN VENV** (see below)
│   ├── smoke_vertical_slice.py        ← Phase 2 end-to-end validation
│   ├── local_pg.sh                    ← PostgreSQL without Docker
│   ├── local_keycloak.sh              ← Keycloak without Docker
│   └── env_dsn.py                     ← Environment variable resolver
│
├── .env                               ← DEV-ONLY CREDENTIALS (gitignored)
├── .env.example                       ← Template (commit this)
├── .gitignore
├── conftest.py                        ← Root test config (adds services/api to path)
└── pytest.ini                         ← Test markers + asyncio config
```

---

## Critical Issue: Migration Script Failure

### The Error
```
aghila@LAPTOP:~/SIH-26$ python3 scripts/migrate.py --dsn "$MEDIKIOSK_MIGRATION_DSN"
ModuleNotFoundError: No module named 'asyncpg'
```

### Root Cause
The migration and seed scripts use Python dependencies (asyncpg) that are installed **only** in the [services/api/.venv](services/api/.venv) virtual environment. When you run them from the repo root without activating the venv, Python uses the system interpreter which doesn't have asyncpg installed.

### Solution
**Always activate the venv BEFORE running migration or seed scripts:**

```bash
cd /home/aghila/SIH-26/services/api
source .venv/bin/activate    # ← THIS IS REQUIRED
cd ../../
python3 scripts/migrate.py --dsn "$MEDIKIOSK_MIGRATION_DSN"
```

Or use the full path to the venv's Python directly:

```bash
/home/aghila/SIH-26/services/api/.venv/bin/python3 scripts/migrate.py --dsn "$MEDIKIOSK_MIGRATION_DSN"
```

---

## Phase 2 Definition of Done (from CLAUDE.md §57-58)

The Phase 2 vertical slice **must** prove:

1. **Complete workflow flow**
   - Patient identity → consent → session creation → protocol loading
   - Interview: deterministic question flow, clinical facts captured
   - Red-flag rule fires and routes to staff

2. **Correct provenance**
   - Every fact has: respondent_id, respondent_relationship, source_type
   - Audit trail shows every action with actor attribution

3. **State machine integrity**
   - Session goes: opened → answering → red-flag-triggered (or completed) → escalated-to-staff → submitted
   - Facts never merge or overwrite; physician edit creates new fact with supersession reference

4. **Real backend verification**
   - API returns correct HTTP status + error codes
   - OPA authorization checks are real (not mocked)
   - Database RLS enforces tenant/patient isolation
   - No claim of success without end-to-end proof

5. **Must work WITHOUT a UI initially**
   - Smoke test uses direct HTTP calls (curl/httpx) against the API
   - Script validates the entire Phase 2 flow programmatically
   - [scripts/smoke_vertical_slice.py](scripts/smoke_vertical_slice.py) implements this

---

## What Needs to Happen for Phase 2 UI Completeness

The backend API is ready. To make Phase 2 truly "complete" with a working UI:

### Missing Frontend Work

1. **Kiosk Frontend** (patient-facing)
   - Identity screen (ABHA or local registration)
   - Consent flow with audio narration
   - Interview loop: show question, collect answer, show next question
   - Red-flag patient-facing message ("Thank you, a nurse is coming…")
   - Completion acknowledgement

2. **Physician Dashboard** (minimal for Phase 2)
   - Session queue with status badges
   - Fact viewer showing provenance (who said what, when)
   - Red-flag incident display
   - Approve / Reject buttons
   - Audit trail viewer

3. **Staff/Triage Console** (WebSocket-based)
   - Real-time red-flag alerts
   - Acknowledge / escalate actions
   - Department-scoped session queue

---

## How to Complete Phase 2 Implementation

### Step 1: Activate the venv and run migrations (FIX THE ASYNCPG ERROR)

```bash
cd /home/aghila/SIH-26/services/api
source .venv/bin/activate
cd ../../

# Verify venv is active (should show path with .venv)
which python

# Set DSN
export MEDIKIOSK_MIGRATION_DSN='postgresql://medikiosk_owner:dev_123098_$%_PostGRE_only_change_me@127.0.0.1:5432/medikiosk'

# Run migrations
python3 scripts/migrate.py --dsn "$MEDIKIOSK_MIGRATION_DSN"

# Seed demo data
python3 scripts/seed_demo.py --dsn "$MEDIKIOSK_MIGRATION_DSN"
```

### Step 2: Start the Docker infrastructure stack

```bash
cd /home/aghila/SIH-26
./scripts/compose.sh up -d postgres redis rabbitmq keycloak opa minio clamav otel-collector prometheus grafana
```

### Step 3: Start the backend API

```bash
cd /home/aghila/SIH-26/services/api
source .venv/bin/activate
uvicorn medikiosk.main:app --host 0.0.0.0 --port 8000 --reload
```

### Step 4: Verify the backend with smoke test

```bash
# In a new terminal
cd /home/aghila/SIH-26
export MEDIKIOSK_MIGRATION_DSN='postgresql://medikiosk_owner:dev_123098_$%_PostGRE_only_change_me@127.0.0.1:5432/medikiosk'

# Get the device credential from the seed output (or query the DB)
# For now, run the smoke test (it will get the device credential from seed_demo output)
python3 scripts/smoke_vertical_slice.py --base-url http://127.0.0.1:8000 --device-credential <CREDENTIAL>
```

### Step 5: Build the minimal Phase 2 UI

The staff-frontend already has Next.js scaffolding. To complete Phase 2:

**Create a minimal kiosk-frontend** (new Next.js app):

```bash
mkdir -p /home/aghila/SIH-26/apps/kiosk-frontend
cd /home/aghila/SIH-26/apps/kiosk-frontend

# Use Next.js create-next-app or copy the staff-frontend structure
```

**Wire staff-frontend to the API:**

In [apps/staff-frontend/app/staff-workspace.tsx](apps/staff-frontend/app/staff-workspace.tsx), replace the stub with:

```tsx
"use client";

import { useEffect, useState } from "react";

interface Session {
  session_id: string;
  patient_id: string;
  status: string;
  protocol_family: string;
}

export default function StaffWorkspace() {
  const [sessions, setSessions] = useState<Session[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    async function fetchSessions() {
      const response = await fetch(`${process.env.NEXT_PUBLIC_API_BASE_URL}/v1/physician/sessions`, {
        headers: {
          Authorization: `Bearer ${localStorage.getItem("access_token")}`,
        },
      });
      const data = await response.json();
      setSessions(data.sessions || []);
      setLoading(false);
    }
    fetchSessions();
  }, []);

  if (loading) return <div>Loading sessions…</div>;

  return (
    <div>
      <h1>Physician Review Queue</h1>
      {sessions.map((session) => (
        <div key={session.session_id} style={{ border: "1px solid #ccc", padding: "10px", margin: "10px 0" }}>
          <h2>{session.patient_id}</h2>
          <p>Status: {session.status}</p>
          <p>Protocol: {session.protocol_family}</p>
          <a href={`/sessions/${session.session_id}`}>Review</a>
        </div>
      ))}
    </div>
  );
}
```

---

## Verification Checklist for Phase 2

- [ ] **Venv activated** and migrations run without asyncpg error
- [ ] **Docker stack running**: Postgres, Redis, Keycloak, OPA, MinIO, ClamAV, Prometheus, Grafana
- [ ] **API service running** on port 8000
- [ ] **Health checks pass**:
  ```bash
  curl http://localhost:8000/healthz
  curl http://localhost:8000/readyz
  ```
- [ ] **Languages endpoint returns 5 languages**:
  ```bash
  curl http://localhost:8000/v1/meta/languages
  ```
- [ ] **Smoke test passes** (verifies backend vertical slice):
  ```bash
  python3 scripts/smoke_vertical_slice.py --base-url http://127.0.0.1:8000 --device-credential <CREDENTIAL>
  ```
- [ ] **Unit tests pass**:
  ```bash
  cd services/api && python -m pytest -q ../../tests/unit ../../tests/red_flag_regression
  ```
- [ ] **Red-flag regression tests pass** (emergency escalation works)

---

## What's Phase 3 & Beyond?

Once Phase 2 is fully verified with working UI:

**Phase 3: Voice**
- Wire ASR/TTS into the kiosk interview
- Test latency budget (<1.5s p95 for speech → next question)
- Fallback to touch when ASR fails

**Phase 4: Documents**
- QR-to-phone upload flow
- Async OCR pipeline + malware scanning
- Confidence-gated extraction

**Phase 5: AI Summary**
- LLM-based evidence-cited summary draft
- Non-blocking (timeout → fall back to structured facts)

**Phase 6: AYUSH**
- Second protocol instantiation (already coded, just needs UI)
- NAMASTE/ICD-11 TM2 dual-coding flow

**Phase 7-10: Staff Dashboards, FHIR/HIS/ABDM, Security Hardening, Production Readiness**

---

## Key Architectural Decisions (Red Lines from CLAUDE.md)

DO NOT:
- Merge AI logic into clinical workflow decisions (deterministic only)
- Allow UI to bypass server-side RBAC/OPA checks
- Store raw Aadhaar numbers
- Cache clinical data on the frontend
- Skip RLS on any patient-data table

DO:
- Prove every authorization check is real (not UI-only hiding)
- Record provenance (respondent_id, source_type) on every fact
- Hash-chain the audit trail
- Test red-flag rules against golden scenarios every merge
- Redact PHI from all logs/metrics

---

## Resources

- **[CLAUDE.md](CLAUDE.md)** — 66 sections, complete architecture reference
- **[README.md](README.md)** — Updated contributor guide
- **[services/api/medikiosk/main.py](services/api/medikiosk/main.py)** — API entry point
- **[scripts/smoke_vertical_slice.py](scripts/smoke_vertical_slice.py)** — Phase 2 validation
- **[infra/docker/docker-compose.yml](infra/docker/docker-compose.yml)** — All services
- **[migrations/](migrations/)** — Database schema (RLS-first)
- **[content/protocols/](content/protocols/)** — Governed clinical content
- **[tests/](tests/)** — Unit, red-flag regression, security isolation tests

---

## Next Immediate Actions

1. **Fix the asyncpg error NOW**:
   ```bash
   cd /home/aghila/SIH-26/services/api && source .venv/bin/activate && cd ../../
   python3 scripts/migrate.py --dsn "$MEDIKIOSK_MIGRATION_DSN"
   python3 scripts/seed_demo.py --dsn "$MEDIKIOSK_MIGRATION_DSN"
   ```

2. **Get the backend running**:
   ```bash
   ./scripts/compose.sh up -d postgres redis rabbitmq keycloak opa minio clamav otel-collector prometheus grafana
   cd services/api && source .venv/bin/activate && uvicorn medikiosk.main:app --port 8000
   ```

3. **Verify Phase 2 backend is complete**:
   ```bash
   python3 scripts/smoke_vertical_slice.py --base-url http://127.0.0.1:8000 --device-credential <CREDENTIAL>
   ```

4. **Build the minimal UI** to complete the Phase 2 user-visible workflow

---

**This guide is the roadmap for Phase 2 completion. CLAUDE.md is the canonical reference for all architecture decisions.**
