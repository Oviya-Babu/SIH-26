# MediKiosk

MediKiosk is a hospital kiosk-first patient case-taking platform designed for high-volume OPD settings. The current implementation is a backend-first, protocol-driven clinical intake system focused on deterministic workflow, consent, safety checks, and auditability.

This repository is now grounded in the architecture and requirements described in [CLAUDE.md](CLAUDE.md). It is not a stale Node/React app; the current implementation is a Python FastAPI monolith with a governed clinical protocol engine, OPA-based authorization, PostgreSQL RLS, and infrastructure for the eventual kiosk/staff frontend surfaces.

---

## Current implementation status

The repo is in a real backend implementation phase, not a blank scaffolding project.

### Implemented so far

- FastAPI API app and modular architecture in [services/api/medikiosk](services/api/medikiosk)
- Clinical protocol engine and deterministic next-question logic
- Session creation, consent gating, and interview flow
- Red-flag engine and emergency escalation logic
- Clinical fact model with provenance and respondent tracking
- Privacy/redaction middleware and observability hooks
- PostgreSQL migration structure and RLS-first database setup
- OPA/Rego policy framework and RBAC checks
- Docker Compose infrastructure for Postgres, Redis, RabbitMQ, Keycloak, OPA, MinIO, ClamAV, Prometheus, Grafana
- Real tests for engine behavior and red-flag regression
- Demo seed data and smoke validation script for the Phase 2 vertical slice

### Fully verified in this repo

The repo’s own test configuration has been run successfully:

```bash
cd /home/aghila/SIH-26/services/api && .venv/bin/python -m pytest -q ../../tests/unit ../../tests/red_flag_regression
```

This completed successfully with exit code 0.

### Still pending / not fully implemented yet

- Real browser frontend apps under [apps](apps) are not present yet
- Full kiosk UI and staff UI not implemented in this workspace
- Voice pipeline, document OCR, AI summary generation, FHIR/HIS/ABDM production integration, and AYUSH full end-to-end flow are not finished as production-ready app surfaces
- The repo is best described as Phase 2 / early Phase 3 territory, not a complete end-user clinical product

---

## Architecture summary

### Core backend

- API: [services/api/medikiosk/main.py](services/api/medikiosk/main.py)
- Clinical protocol engine: [services/api/medikiosk/modules/clinical_protocol](services/api/medikiosk/modules/clinical_protocol)
- Session logic: [services/api/medikiosk/modules/session](services/api/medikiosk/modules/session)
- Triage/red flags: [services/api/medikiosk/modules/triage](services/api/medikiosk/modules/triage)
- Consent: [services/api/medikiosk/modules/consent](services/api/medikiosk/modules/consent)
- Document handling: [services/api/medikiosk/modules/document](services/api/medikiosk/modules/document)
- Security: [services/api/medikiosk/security](services/api/medikiosk/security)

### Infrastructure and governance

- Docker stack: [infra/docker/docker-compose.yml](infra/docker/docker-compose.yml)
- Migrations: [migrations](migrations)
- OPA policies: [policies/opa](policies/opa)
- Test suite: [tests](tests)
- Controlled content: [content](content)

### Demo and validation

- Seed demo data: [scripts/seed_demo.py](scripts/seed_demo.py)
- Vertical slice smoke test: [scripts/smoke_vertical_slice.py](scripts/smoke_vertical_slice.py)

---

## Prerequisites

- Python 3.12+
- Docker + Docker Compose
- Git
- Optional: a local terminal with bash

---

## Quick start with requirements.txt

The project includes a Python dependency file for the backend service at [services/api/requirements.txt](services/api/requirements.txt).

### 1) Create and activate a virtual environment

```bash
cd /home/aghila/SIH-26/services/api
python3.12 -m venv .venv
source .venv/bin/activate
```

### 2) Install dependencies

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

This installs the FastAPI service dependencies and the dev/test tools needed for local verification.

---

## Start the project locally

### Option A: Docker-based local stack (recommended)

From the repo root:

```bash
cd /home/aghila/SIH-26
./scripts/compose.sh up -d postgres redis rabbitmq keycloak opa minio clamav otel-collector prometheus grafana
```

Then run migrations:

```bash
export MEDIKIOSK_MIGRATION_DSN='postgresql://medikiosk_owner:dev_123098_$%_PostGRE_only_change_me@127.0.0.1:5432/medikiosk'
python scripts/migrate.py --dsn "$MEDIKIOSK_MIGRATION_DSN"
```

Seed demo data:

```bash
python scripts/seed_demo.py --dsn "$MEDIKIOSK_MIGRATION_DSN"
```

Start the API:

```bash
cd /home/aghila/SIH-26/services/api
source .venv/bin/activate
uvicorn medikiosk.main:app --host 0.0.0.0 --port 8000 --reload
```

### Option B: run only the backend service without Docker

This is possible for local FastAPI testing if the required services are already running elsewhere, but the canonical development path is still the Docker stack above.

---

## Verify the service is up

### Health checks

```bash
curl http://localhost:8000/healthz
curl http://localhost:8000/readyz
```

### Metadata endpoints

```bash
curl http://localhost:8000/v1/meta/languages
```

### Smoke test for the Phase 2 vertical slice

The project includes a live smoke test that exercises the core workflow:

```bash
cd /home/aghila/SIH-26
python scripts/smoke_vertical_slice.py --base-url http://127.0.0.1:8000 --device-credential "<device_credential_from_seed_demo>"
```

This validates the end-to-end sequence:

- kiosk device auth
- local registration
- consent
- session creation
- deterministic question flow
- clinical fact creation
- red-flag escalation
- session completion behavior

---

## Run tests

### Unit + red-flag regression

```bash
cd /home/aghila/SIH-26/services/api
source .venv/bin/activate
python -m pytest -q ../../tests/unit ../../tests/red_flag_regression
```

### Security tests

```bash
python -m pytest -q ../../tests/security
```

---

## What contributors should do next

The repo is already strong in the foundational clinical engine and security model. The next useful workstream is to continue in the sequence defined by [CLAUDE.md](CLAUDE.md):

1. Finish the real frontend surfaces for kiosk and staff interfaces
2. Complete the voice pipeline and multilingual interaction flow
3. Validate OCR + document processing end-to-end
4. Complete the AI summary evidence-citing path
5. Add the AYUSH protocol use case and NAMASTE mapping flow
6. Finalize FHIR/HIS/ABDM adapter testing and sandbox export
7. Hardening and DPDP / VAPT readiness with real legal and security review

---

## Important note

The project is intentionally backend-first and security-first. A lot of the real value is in the deterministic clinical engine, the audit trail, RBAC model, red-flag logic, and the compliance-oriented architecture. The browser UI is still the missing layer to make the system a fully user-visible product.

For contributors, the correct starting point is:

- [CLAUDE.md](CLAUDE.md)
- [services/api](services/api)
- [migrations](migrations)
- [tests](tests)
- [scripts/smoke_vertical_slice.py](scripts/smoke_vertical_slice.py)
