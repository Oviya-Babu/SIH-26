# MediKiosk — Patient Clinical Intake Platform

[![Python](https://img.shields.io/badge/Python-3.12+-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115+-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![Next.js](https://img.shields.io/badge/Next.js-16.3+-000000?logo=next.js&logoColor=white)](https://nextjs.org/)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-RLS%20Enabled-4169E1?logo=postgresql&logoColor=white)](https://www.postgresql.org/)
[![OPA](https://img.shields.io/badge/OPA-Rego%20AuthZ-7D3C98?logo=openpolicyagent&logoColor=white)](https://www.openpolicyagent.org/)
[![License](https://img.shields.io/badge/Status-SIH%202024%2F2026-brightgreen)](#)

> **Official Smart India Hackathon (SIH) Project**  
> **Problem Statement 4:** *Patient Case-Taking Software*  
> **Organization:** **All India Institute of Ayurveda (AIIA), Ministry of Ayush**  
> **Core Setting:** High-volume Indian Outpatient Departments (OPDs) handling 4,000–10,000 patients/day with 2–5 minute physician consultation windows.

MediKiosk is a **hospital kiosk-first, multimodal clinical case-taking platform**. A patient walks up to a touchscreen kiosk with zero prior registration, completes an adaptive voice + touch clinical interview across allopathic (SOCRATES) or Ayurvedic intake (Trividha, Ashtavidha, Dashavidha Pariksha, Ahara-Vihara, Nidana-Samprapti), digitizes prior paper records, and generates an evidence-cited clinical draft for immediate physician review.

---

## Architecture at a Glance

```
                ┌─────────────────────────────────────────────────────────┐
                │             Patient Kiosk Touchscreen & Audio           │
                │        (:8000 /kiosk — English, Hindi, Ta, Te, Ml)      │
                └────────────────────────────┬────────────────────────────┘
                                             │ Web Audio / Touch
                                             ▼
┌─────────────────────────────────────────────────────────────────────────────────────────┐
│                           AI Gateway Inference Tier (:8100)                             │
│   • Silero VAD v5 (ONNX Runtime, 32ms frames)                                           │
│   • ASR: faster-whisper-small / whisper-large-v3-turbo (En/Hi) + indic-conformer (Ta/Te/Ml)│
│   • NLU: Multilingual Clinical Multi-Slot Extractor (_SOCRATES_SYNONYMS + MiniLM-L12)   │
│   • TTS: Pre-cached WAV retrieval (<15ms) + dynamic synthesis fallback                  │
└────────────────────────────────────────────┬────────────────────────────────────────────┘
                                             │ Validated Slots (Site, Character, Duration)
                                             ▼
┌─────────────────────────────────────────────────────────────────────────────────────────┐
│                    MediKiosk API Monolith & Protocol Core (:8000)                       │
│   • Deterministic Protocol Engine: Protocol = (C, F, D, R, O)                           │
│   • Evaluates Declarative Predicates D(f, state) -> Dynamic NextField Calculation       │
│   • Red-Flag Emergency Detection & AMPLE Fast Path                                      │
│   • PostgreSQL with Row-Level Security (RLS) + Append-Only Hash-Chained Audit Trail     │
│   • OPA / Rego Policy Enforcement (Deny-by-default RBAC + ABAC)                         │
└────────────────────────────────────────────┬────────────────────────────────────────────┘
                                             │ Cited Facts (100% Provenance)
                                             ▼
┌─────────────────────────────────────────────────────────────────────────────────────────┐
│                  Staff & Physician Review Workspace (:3200 Next.js)                     │
│   • Instant Structured Summary with Fact Citation Inspector                             │
│   • Physician Authority Gate: Accept / Amend / Reject / Approve (Draft -> Approved)    │
│   • NAMASTE & ICD-11 TM2 Ayurvedic Diagnosis Confirmation                               │
│   • Real-Time Nurse Red-Flag Emergency Escalation Queue (WebSocket)                     │
└─────────────────────────────────────────────────────────────────────────────────────────┘
```

### Strict Non-Negotiable Boundaries (CLAUDE.md Red Lines)
1. **AI is strictly perceptual:** AI identifies *what the patient said* (ASR and multi-slot NLU). The deterministic Protocol Engine decides *what to ask next*.
2. **AI never possesses clinical authority:** AI never selects questions, never decides $NextField$, never decides red flags, never prescribes, never diagnoses, and never writes directly to the database.
3. **Database Isolation:** AI workers have **no network route to PostgreSQL** and zero database credentials.
4. **Physician-in-the-Loop:** No clinical record can be finalized or exported to ABDM/FHIR without explicit physician approval.

---

## Prerequisites

Before starting, ensure you have the following installed on your machine:
- **Operating System:** Linux (Ubuntu 22.04+ recommended), macOS, or Windows WSL2
- **Docker & Docker Compose v2+** (`docker compose version`)
- **Python 3.12+** (`python3 --version`)
- **Node.js 18+ and npm** (`node -v && npm -v`)
- **Git & Bash**

---

## Quick Start Guide (Zero to Running in 5 Minutes)

Follow this step-by-step sequence to get the entire MediKiosk platform up and running.

### Step 1: Clone Repository & Configure Environment

```bash
git clone <repository_url> medikiosk
cd medikiosk

# The project includes a pre-configured .env file for local development.
# If creating a fresh one:
cp .env.example .env
```

### Step 2: Start Infrastructure via Docker Compose

Start the database, message broker, authentication, and authorization services:

```bash
./scripts/compose.sh up -d postgres redis rabbitmq keycloak opa minio clamav otel-collector prometheus grafana
```

Verify that all containers are healthy:
```bash
docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
```

> [!NOTE]
> Ensure `medikiosk-opa` is running and has compiled `/policies/authz.rego`. You can verify OPA health anytime with:
> `curl -s http://localhost:8181/v1/policies`

---

### Step 3: Setup Python Environment & Run Database Migrations

Create your virtual environment and install dependencies:

```bash
# Create and activate virtual environment at repository root
python3.12 -m venv .venv
source .venv/bin/activate

# Install backend API dependencies
pip install --upgrade pip
pip install -r services/api/requirements.txt

# Install AI Gateway dependencies
pip install -e services/ai-gateway
```

Run database migrations and seed demonstration data:

```bash
# Export properly URL-encoded connection strings
eval "$(python3 scripts/env_dsn.py --export)"

# Run PostgreSQL migrations (RLS enabled from migration 0001)
python3 scripts/migrate.py --dsn "$MEDIKIOSK_MIGRATION_DSN"

# Seed default tenants, departments (General Medicine & AYUSH), devices, and users
python3 scripts/seed_demo.py --dsn "$MEDIKIOSK_MIGRATION_DSN"
```

---

### Step 4: Start the Application Services

Open **3 terminal tabs** to run the services:

#### Terminal 1: AI Gateway Service (Port 8100)
```bash
cd services/ai-gateway
source ../../.venv/bin/activate
PYTHONPATH=. uvicorn medikiosk_ai.main:app --host 0.0.0.0 --port 8100 --reload
```
*Health Check:* `curl http://localhost:8100/healthz`

#### Terminal 2: MediKiosk Backend API & Kiosk UI (Port 8000)
```bash
source .venv/bin/activate
eval "$(python3 scripts/env_dsn.py --export)"
uvicorn medikiosk.main:app --app-dir services/api --host 0.0.0.0 --port 8000 --reload
```
*Health Check:* `curl http://localhost:8000/healthz`

#### Terminal 3: Staff & Physician Frontend (Port 3200)
```bash
cd apps/staff-frontend
npm install
npm run dev
```
*Accessible at:* `http://localhost:3200`

---

## Accessing the Platform

| Application Surface | URL | Default Credentials / Role | Notes |
|---|---|---|---|
| **Patient Kiosk UI** | [http://localhost:8000](http://localhost:8000) (or `/kiosk`) | Public Patient Access | Touchscreen UI, microphone voice capture, audio prompts |
| **Staff & Physician Dashboard** | [http://localhost:3200](http://localhost:3200) | Physician / Nurse / Admin | Clinical summary review, red-flag queue, fact approval |
| **FastAPI Interactive Docs** | [http://localhost:8000/docs](http://localhost:8000/docs) | Developer / API Docs | Swagger UI with OpenAPI 3.1 specification |
| **AI Gateway Service** | [http://localhost:8100/healthz](http://localhost:8100/healthz) | Internal API | Speech transcription, NLU extraction, and TTS audio synthesis |
| **Grafana Observability** | [http://localhost:3000](http://localhost:3000) | `admin` / `.env` password | Real-time system latency, throughput, and error metrics |
| **Keycloak Identity Console** | [http://localhost:8080](http://localhost:8080) | `admin` / `.env` password | OIDC realm `medikiosk`, role management, and MFA |

---

## Live Walkthrough: Testing the Clinical Intake

### 1. Patient Voice & Touch Intake (Kiosk)
1. Open [http://localhost:8000](http://localhost:8000) in Chrome or Edge.
2. Select your language (**English**, **हिंदी**, **தமிழ்**, **తెలుగు**, or **മലയാളം**) and click **"Start Intake"**.
3. Choose a Department:
   - **General Medicine:** Standard SOCRATES clinical intake.
   - **AYUSH Ayurveda:** Dashavidha Pariksha, Ahara-Vihara habits, and Prakriti indicators.
4. Check the consent box and tap **"✓ I Agree & Start Interview"**.
5. Tap the **Blue Microphone Button** and speak naturally:
   > *"I have had sharp chest pain since morning and it gets worse when I breathe."*
6. **Watch the Adaptive Engine in Real Time:**
   - Silero VAD detects end of speech.
   - Faster-whisper transcribes audio in memory (raw audio is purged immediately).
   - Multilingual NLU simultaneously extracts **Site (`chest`)**, **Character (`sharp`)**, **Duration (`1 day`)**, and **Aggravating factor (`breathing`)**.
   - All 4 clinical facts are committed in a single atomic database transaction.
   - The engine skips past all 4 answered questions and automatically advances to Radiation/Severity with voice output!

### 2. Handling Uncertainty ("Don't Know" / Skipped Answers)
If you speak *"I don't know"*, *"not sure"*, *"maloom nahi"*, or *"theriyathu"*, the engine records `skip_reason = SkipReason.PATIENT_UNSURE`, advances smoothly without stalling, and marks the question as unanswered on the physician dashboard.

### 3. Red-Flag Emergency Escalation
If you report a critical symptom (e.g. *severe chest pain radiating to the left arm* or *difficulty breathing*):
- The deterministic Red-Flag engine fires instantly (<50ms).
- The kiosk switches to the calm AMPLE fast path.
- The Nurse console on `http://localhost:3200` triggers a real-time WebSocket alert.

### 4. Physician Review Workspace
1. Navigate to [http://localhost:3200](http://localhost:3200).
2. Open the active patient session.
3. Review the AI-drafted clinical summary. Notice that **every generated sentence cites a concrete `clinical_fact.id`**.
4. Edit any fact (creates a new versioned fact, preserving full audit history).
5. Confirm NAMASTE / ICD-11 TM2 diagnosis codes.
6. Click **"Approve"** to finalize the record and queue FHIR R4 / ABDM export.

---

## Verification & Automated Tests

To run the complete automated test suite:

```bash
# Run unit tests and clinical red-flag safety regression suite
python3 -m pytest tests/unit tests/red_flag_regression -v

# Run authorization and security tests
python3 -m pytest tests/security -v

# Run the end-to-end vertical slice smoke test
python3 scripts/smoke_vertical_slice.py --base-url http://127.0.0.1:8000
```

---

## Troubleshooting & Common Pitfalls

### 1. Browser Autoplay Audio Policy
Modern web browsers (Chrome, Firefox, Safari) prevent automatic sound playback until a user performs an interaction (click or tap) on the page.  
*Solution:* Ensure you tap "Start Intake" or the microphone button; this automatically initializes and resumes the Web Audio `AudioContext`.

### 2. "Error loading question" Toast on Kiosk
If the kiosk shows "Error loading question", the OPA container may not have loaded its policy file.  
*Solution:* Restart the OPA container to reload and compile `authz.rego`:
```bash
docker restart medikiosk-opa
curl http://localhost:8181/v1/policies  # Verify policies are loaded
```

### 3. Port Conflicts
Ensure ports `8000`, `8100`, `3200`, `5432`, and `8181` are not occupied by other software:
```bash
ss -tulpn | grep -E ':(8000|8100|3200|5432|8181)'
```

---

## Canonical Architecture Document

For deep architectural specifications, data models, DPDP Act 2023 compliance controls, FHIR mappings, and cloud GPU deployment configurations, refer to the single authoritative source of truth:

👉 **[CLAUDE.md](CLAUDE.md)**
