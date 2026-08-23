# MediKiosk

> **AI-Powered Pre-Consultation Clinical Intake & Medical-Record Intelligence Platform**

MediKiosk is an AI-assisted, protocol-governed, evidence-backed, and physician-verified clinical intake system designed to capture structured patient context before consultations begin.

---

## 🏗️ Architecture & Features

- **Protocol-Governed State Machine**: Deterministic question engine dynamically guides the interview based on clinical ontologies (General Medicine v1) without allowing LLM hallucinations to determine clinical paths.
- **Incremental Red-Flag Safety Engine**: Instant emergency rule evaluations (Acute Coronary Syndrome, severe dyspnoea, extreme pain) executed after every single answer with automated triage routing.
- **Longitudinal Clinical Timeline**: Merges patient self-reporting and historical records with exact provenance.
- **Evidence-Based Summary Generation**: Produces physician-ready summaries with 100% cited atomic fact tracing.
- **Conflict & Contradiction Detection**: Flags discrepancies between patient claims and medical documentation for physician adjudication.
- **Multi-Role Clinical Workspaces**:
  - 🏥 **Patient Kiosk / Web**: Multilingual conversational voice & touch UI with specialized clinical widgets (Yes/No, Pain Severity slider with emojis, Body Map selector, Multi-Select, Duration Picker).
  - 👨‍⚕️ **Physician Review Dashboard**: Priority triage queue, contradiction callouts, per-section Accept/Edit/Reject workflow, and clinical facts explorer.
  - 🩺 **Staff / Triage Console**: Real-time red-flag alerts with SLA timers, acknowledge/resolve actions, and session monitoring.
  - 📊 **Hospital Administration**: Analytics overview, clinical protocol management, safety rule inspection, and hash-chained audit trails.

---

## 🛠️ Tech Stack

- **Monorepo**: npm workspaces
- **Frontend**: React 19, TypeScript, Vite, React Router, Socket.IO Client, Vanilla CSS Design System
- **Backend**: Node.js, Express, Socket.IO, TypeScript, Helmet, CORS
- **Shared Package**: `@medikiosk/shared` (end-to-end type safety & clinical constants)

---

## 🚀 Quick Start

### Prerequisites
- Node.js (v18+)
- npm (v9+)

### Installation

```bash
# Clone the repository
git clone <your-repo-url>
cd SIH

# Install all workspace dependencies
npm install
```

### Running Locally

```bash
# Start both Backend API and Frontend Client concurrently
npm run dev
```

Or start them individually:

```bash
# Start backend API (Port 3001)
npm run dev:server

# Start frontend application (Port 5173)
npm run dev:client
```

- **Frontend App**: `http://localhost:5173/`
- **Backend API**: `http://localhost:3001/v1`
- **WebSocket Gateway**: `ws://localhost:3001`

---

## 📂 Project Structure

```
SIH/
├── apps/
│   ├── client/          # Vite + React + TypeScript web application
│   │   ├── src/
│   │   │   ├── pages/   # Landing, PatientIntake, PhysicianDashboard, StaffConsole, AdminPanel
│   │   │   ├── services/# REST API & WebSocket client
│   │   │   └── index.css# Comprehensive clinical design system
│   │   └── vite.config.ts
│   └── server/          # Express + Socket.IO API server
│       └── src/
│           ├── db/      # In-memory clinical facts store
│           ├── engines/ # Question Engine, Red-Flag Engine, Timeline Engine, Conflict Detector, Summary Generator
│           ├── data/    # Clinical protocols & red-flag rule definitions
│           └── index.ts # API router & WebSocket handlers
├── packages/
│   └── shared/          # Central domain models, clinical ontology types, constants
├── package.json         # Workspace root scripts
└── tsconfig.json        # Base TypeScript configuration
```

---

## 🔒 Security & Privacy

- Strict RBAC (Patient, Physician, Nurse, Hospital Admin, Clinical Admin)
- Immutable, tamper-evident hash-chained audit logging
- DPDP & ABDM-compliant consent management gates
