// ─── In-Memory Data Store ────────────────────────────────────────────────────
// MVP replacement for PostgreSQL — all data lives here until DB integration

import type {
  Patient, Session, Encounter, Consent, Answer, ClinicalFact,
  MedicalDocument, ClinicalSummary, RedFlagAlert, TimelineEvent,
  AuditEvent, Protocol, RedFlagRule, User
} from '@medikiosk/shared';

class DataStore {
  patients: Map<string, Patient> = new Map();
  sessions: Map<string, Session> = new Map();
  encounters: Map<string, Encounter> = new Map();
  consents: Map<string, Consent> = new Map();
  answers: Map<string, Answer> = new Map();
  clinicalFacts: Map<string, ClinicalFact> = new Map();
  documents: Map<string, MedicalDocument> = new Map();
  summaries: Map<string, ClinicalSummary> = new Map();
  redFlagAlerts: Map<string, RedFlagAlert> = new Map();
  timelineEvents: Map<string, TimelineEvent> = new Map();
  auditEvents: AuditEvent[] = []; // append-only
  protocols: Map<string, Protocol> = new Map();
  redFlagRules: Map<string, RedFlagRule> = new Map();
  users: Map<string, User> = new Map();

  // Idempotency key tracking
  private processedKeys: Set<string> = new Set();

  constructor() {
    this.seedDemoData();
  }

  checkIdempotency(key: string): boolean {
    if (this.processedKeys.has(key)) return true;
    this.processedKeys.add(key);
    return false;
  }

  // Query helpers
  getSessionsByPatient(patientId: string): Session[] {
    return [...this.sessions.values()].filter(s => s.patientId === patientId);
  }

  getAnswersBySession(sessionId: string): Answer[] {
    return [...this.answers.values()].filter(a => a.sessionId === sessionId);
  }

  getFactsBySession(sessionId: string): ClinicalFact[] {
    return [...this.clinicalFacts.values()].filter(f => f.sessionId === sessionId);
  }

  getDocumentsBySession(sessionId: string): MedicalDocument[] {
    return [...this.documents.values()].filter(d => d.sessionId === sessionId);
  }

  getAlertsBySession(sessionId: string): RedFlagAlert[] {
    return [...this.redFlagAlerts.values()].filter(a => a.sessionId === sessionId);
  }

  getActiveAlerts(tenantId?: string): RedFlagAlert[] {
    return [...this.redFlagAlerts.values()].filter(a =>
      (a.status === 'active' || a.status === 'escalated') &&
      (!tenantId || a.tenantId === tenantId)
    );
  }

  getTimelineBySession(sessionId: string): TimelineEvent[] {
    return [...this.timelineEvents.values()]
      .filter(e => e.sessionId === sessionId)
      .sort((a, b) => {
        if (a.date && b.date) return new Date(b.date).getTime() - new Date(a.date).getTime();
        if (a.date) return -1;
        if (b.date) return 1;
        return 0;
      });
  }

  getConsentsBySession(sessionId: string): Consent[] {
    return [...this.consents.values()].filter(c => c.sessionId === sessionId);
  }

  getSummaryBySession(sessionId: string): ClinicalSummary | undefined {
    return [...this.summaries.values()].find(s => s.sessionId === sessionId);
  }

  getActiveProtocols(): Protocol[] {
    return [...this.protocols.values()].filter(p => p.status === 'active');
  }

  getActiveRedFlagRules(): RedFlagRule[] {
    return [...this.redFlagRules.values()].filter(r => r.active);
  }

  queryAuditEvents(params: {
    tenantId?: string;
    eventType?: string;
    patientId?: string;
    sessionId?: string;
    limit?: number;
    offset?: number;
  }): { events: AuditEvent[]; total: number } {
    let filtered = [...this.auditEvents];
    if (params.tenantId) filtered = filtered.filter(e => e.tenantId === params.tenantId);
    if (params.eventType) filtered = filtered.filter(e => e.eventType === params.eventType);
    if (params.patientId) filtered = filtered.filter(e => e.patientId === params.patientId);
    if (params.sessionId) filtered = filtered.filter(e => e.sessionId === params.sessionId);

    const total = filtered.length;
    const offset = params.offset || 0;
    const limit = params.limit || 50;
    filtered = filtered.slice(offset, offset + limit);

    return { events: filtered, total };
  }

  private seedDemoData() {
    // Seed demo users
    const demoUsers: User[] = [
      {
        id: 'user-physician-1', tenantId: 'hospital-1', email: 'dr.sharma@hospital.in',
        name: 'Dr. Arun Sharma', role: 'physician', department: 'General Medicine',
        specialization: 'Internal Medicine', active: true, createdAt: new Date().toISOString()
      },
      {
        id: 'user-nurse-1', tenantId: 'hospital-1', email: 'nurse.priya@hospital.in',
        name: 'Priya Nair', role: 'nurse', department: 'OPD Triage',
        active: true, createdAt: new Date().toISOString()
      },
      {
        id: 'user-admin-1', tenantId: 'hospital-1', email: 'admin@hospital.in',
        name: 'Rajesh Kumar', role: 'hospital_admin', department: 'Administration',
        active: true, createdAt: new Date().toISOString()
      },
      {
        id: 'user-clinical-admin-1', tenantId: 'hospital-1', email: 'clinical.admin@hospital.in',
        name: 'Dr. Meera Iyer', role: 'clinical_admin', department: 'Clinical Governance',
        active: true, createdAt: new Date().toISOString()
      },
    ];
    demoUsers.forEach(u => this.users.set(u.id, u));

    // Seed demo patients with completed/in-progress sessions
    const demoPatients: Patient[] = [
      {
        id: 'patient-1', hospitalLocalId: 'MRN-2024-0001', tenantId: 'hospital-1',
        firstName: 'Ramesh', lastName: 'Gupta', dateOfBirth: '1965-03-15', sex: 'male', age: 59,
        phone: '+91-9876543210', language: 'hi', address: 'Block B, Sector 12, Noida',
        createdAt: new Date(Date.now() - 3600000).toISOString(), updatedAt: new Date().toISOString()
      },
      {
        id: 'patient-2', hospitalLocalId: 'MRN-2024-0002', tenantId: 'hospital-1',
        firstName: 'Lakshmi', lastName: 'Devi', dateOfBirth: '1978-08-22', sex: 'female', age: 46,
        phone: '+91-9876543211', language: 'en', address: '14, Gandhi Road, Chennai',
        createdAt: new Date(Date.now() - 7200000).toISOString(), updatedAt: new Date().toISOString()
      },
      {
        id: 'patient-3', hospitalLocalId: 'MRN-2024-0003', tenantId: 'hospital-1',
        firstName: 'Arjun', lastName: 'Patel', dateOfBirth: '1990-11-05', sex: 'male', age: 34,
        phone: '+91-9876543212', language: 'en',
        createdAt: new Date(Date.now() - 1800000).toISOString(), updatedAt: new Date().toISOString()
      },
    ];
    demoPatients.forEach(p => this.patients.set(p.id, p));

    // Seed sessions — one completed with summary ready, one in-progress, one with red flag
    const demoSessions: Session[] = [
      {
        id: 'session-1', patientId: 'patient-1', tenantId: 'hospital-1', encounterId: 'enc-1',
        protocolId: 'general_medicine_v1', protocolVersion: '1.0', status: 'awaiting_review',
        language: 'hi', channel: 'kiosk', completenessScore: 0.85, startedAt: new Date(Date.now() - 3600000).toISOString(),
        completedAt: new Date(Date.now() - 1800000).toISOString(), lastActivityAt: new Date(Date.now() - 1800000).toISOString(),
        answeredQuestions: ['q-cc', 'q-onset', 'q-location', 'q-severity', 'q-character', 'q-radiation', 'q-duration', 'q-pmh-dm', 'q-pmh-htn', 'q-med-current', 'q-allergy'],
      },
      {
        id: 'session-2', patientId: 'patient-2', tenantId: 'hospital-1', encounterId: 'enc-2',
        protocolId: 'general_medicine_v1', protocolVersion: '1.0', status: 'intake_active',
        language: 'en', channel: 'tablet', completenessScore: 0.45, startedAt: new Date(Date.now() - 1200000).toISOString(),
        lastActivityAt: new Date(Date.now() - 60000).toISOString(), currentQuestionId: 'q-severity',
        answeredQuestions: ['q-cc', 'q-onset', 'q-location'],
      },
      {
        id: 'session-3', patientId: 'patient-3', tenantId: 'hospital-1', encounterId: 'enc-3',
        protocolId: 'general_medicine_v1', protocolVersion: '1.0', status: 'intake_paused',
        language: 'en', channel: 'phone', completenessScore: 0.3, startedAt: new Date(Date.now() - 900000).toISOString(),
        lastActivityAt: new Date(Date.now() - 300000).toISOString(), currentQuestionId: 'q-character',
        answeredQuestions: ['q-cc', 'q-onset'],
      },
    ];
    demoSessions.forEach(s => this.sessions.set(s.id, s));

    // Seed clinical facts for patient-1 (completed session)
    const demoFacts: ClinicalFact[] = [
      {
        id: 'fact-1', sessionId: 'session-1', category: 'chief_complaint', conceptCode: 'chest_pain',
        conceptLabel: 'Chest Pain', valueRaw: 'सीने में दर्द सुबह से है', valueNormalized: 'Chest pain since morning',
        confidence: 0.92, source: 'patient_reported', provenanceRef: 'ans-1',
        isConflicting: false, timestamp: new Date(Date.now() - 3600000).toISOString()
      },
      {
        id: 'fact-2', sessionId: 'session-1', category: 'symptom', conceptCode: 'pain_location',
        conceptLabel: 'Pain Location', valueRaw: 'बायीं तरफ', valueNormalized: 'Left chest',
        valueStructured: { region: 'chest_left' },
        confidence: 0.88, source: 'patient_reported', provenanceRef: 'ans-2',
        isConflicting: false, timestamp: new Date(Date.now() - 3500000).toISOString()
      },
      {
        id: 'fact-3', sessionId: 'session-1', category: 'symptom', conceptCode: 'pain_severity',
        conceptLabel: 'Pain Severity', valueRaw: '7', valueNormalized: '7/10',
        confidence: 0.95, source: 'patient_reported', provenanceRef: 'ans-3',
        isConflicting: false, timestamp: new Date(Date.now() - 3400000).toISOString()
      },
      {
        id: 'fact-4', sessionId: 'session-1', category: 'symptom', conceptCode: 'pain_character',
        conceptLabel: 'Pain Character', valueRaw: 'crushing, heavy', valueNormalized: 'Crushing/pressing',
        confidence: 0.85, source: 'patient_reported', provenanceRef: 'ans-4',
        isConflicting: false, timestamp: new Date(Date.now() - 3300000).toISOString()
      },
      {
        id: 'fact-5', sessionId: 'session-1', category: 'symptom', conceptCode: 'pain_radiation',
        conceptLabel: 'Pain Radiation', valueRaw: 'left arm', valueNormalized: 'Radiates to left arm',
        confidence: 0.9, source: 'patient_reported', provenanceRef: 'ans-5',
        isConflicting: false, timestamp: new Date(Date.now() - 3200000).toISOString()
      },
      {
        id: 'fact-6', sessionId: 'session-1', category: 'past_medical_history', conceptCode: 'diabetes_mellitus',
        conceptLabel: 'Diabetes Mellitus', valueRaw: 'no', valueNormalized: 'Denies diabetes mellitus',
        confidence: 0.95, source: 'patient_reported', provenanceRef: 'ans-6',
        isConflicting: true, conflictGroupId: 'conflict-1',
        timestamp: new Date(Date.now() - 3100000).toISOString()
      },
      {
        id: 'fact-7', sessionId: 'session-1', category: 'past_medical_history', conceptCode: 'hypertension',
        conceptLabel: 'Hypertension', valueRaw: 'yes, 5 years', valueNormalized: 'Hypertension — 5 years',
        confidence: 0.93, source: 'patient_reported', provenanceRef: 'ans-7',
        isConflicting: false, timestamp: new Date(Date.now() - 3000000).toISOString()
      },
      {
        id: 'fact-8', sessionId: 'session-1', category: 'medication', conceptCode: 'amlodipine',
        conceptLabel: 'Amlodipine', valueRaw: 'Amlodipine 5mg daily', valueNormalized: 'Amlodipine 5mg OD',
        valueStructured: { drug: 'Amlodipine', dose: '5mg', frequency: 'OD', route: 'oral' },
        confidence: 0.91, source: 'patient_reported', provenanceRef: 'ans-8',
        isConflicting: false, timestamp: new Date(Date.now() - 2900000).toISOString()
      },
      {
        id: 'fact-9', sessionId: 'session-1', category: 'past_medical_history', conceptCode: 'diabetes_mellitus',
        conceptLabel: 'Diabetes Mellitus (Document)', valueRaw: 'Type 2 Diabetes Mellitus',
        valueNormalized: 'Type 2 Diabetes Mellitus (documented)',
        confidence: 0.87, source: 'document_extracted', provenanceRef: 'doc-entity-1',
        isConflicting: true, conflictGroupId: 'conflict-1',
        timestamp: new Date(Date.now() - 2800000).toISOString(),
        metadata: { documentSource: 'Discharge Summary, City Hospital, 12 Mar 2023' }
      },
      {
        id: 'fact-10', sessionId: 'session-1', category: 'medication', conceptCode: 'metformin',
        conceptLabel: 'Metformin', valueRaw: 'Tab. Metfor 500mg BD',
        valueNormalized: 'Metformin 500mg BID',
        valueStructured: { drug: 'Metformin', dose: '500mg', frequency: 'BID', route: 'oral' },
        confidence: 0.72, source: 'document_extracted', provenanceRef: 'doc-entity-2',
        isConflicting: false, timestamp: new Date(Date.now() - 2700000).toISOString(),
        metadata: { documentSource: 'Prescription, City Hospital, 12 Mar 2023' }
      },
      {
        id: 'fact-11', sessionId: 'session-1', category: 'allergy', conceptCode: 'sulfa_allergy',
        conceptLabel: 'Sulfonamide Allergy', valueRaw: 'sulfa drugs', valueNormalized: 'Sulfonamide antibiotics — allergy',
        confidence: 0.89, source: 'patient_reported', provenanceRef: 'ans-9',
        isConflicting: false, timestamp: new Date(Date.now() - 2600000).toISOString()
      },
    ];
    demoFacts.forEach(f => this.clinicalFacts.set(f.id, f));

    // Seed red flag alert for patient-1
    const demoAlerts: RedFlagAlert[] = [
      {
        id: 'alert-1', sessionId: 'session-1', patientId: 'patient-1', tenantId: 'hospital-1',
        ruleId: 'rule-acs', ruleName: 'Possible Acute Coronary Syndrome Pattern',
        severity: 'critical', status: 'active',
        matchedConditions: {
          chest_pain: true, pain_location: 'left_chest', pain_radiation: 'left_arm',
          pain_character: 'crushing', severity_gte: 7
        },
        staffMessage: 'Patient presents with left-sided crushing chest pain (7/10) radiating to left arm. Known hypertensive. Pattern consistent with possible ACS — requires immediate triage assessment.',
        firedAt: new Date(Date.now() - 3200000).toISOString(), escalationLevel: 0,
      },
    ];
    demoAlerts.forEach(a => this.redFlagAlerts.set(a.id, a));

    // Seed timeline events
    const demoTimeline: TimelineEvent[] = [
      {
        id: 'tl-1', sessionId: 'session-1', factId: 'fact-7', date: '2019-01-01',
        dateApproximate: true, dateLabel: 'Approximately 5 years ago',
        category: 'Diagnosis', title: 'Hypertension diagnosed',
        description: 'Patient reports being diagnosed with hypertension approximately 5 years ago.',
        source: 'patient_reported', provenanceRef: 'ans-7',
      },
      {
        id: 'tl-2', sessionId: 'session-1', factId: 'fact-9', date: '2023-03-12',
        dateApproximate: false, dateLabel: '12 Mar 2023',
        category: 'Hospitalization', title: 'Discharge — City Hospital',
        description: 'Discharge summary mentions Type 2 Diabetes Mellitus.',
        source: 'document_extracted', provenanceRef: 'doc-entity-1',
        metadata: { documentSource: 'Discharge Summary, City Hospital' }
      },
      {
        id: 'tl-3', sessionId: 'session-1', factId: 'fact-1', dateLabel: 'Today',
        dateApproximate: false, date: new Date().toISOString().split('T')[0],
        category: 'Current Visit', title: 'Chest pain since morning',
        description: 'Left-sided crushing chest pain (7/10), radiating to left arm. Started this morning.',
        source: 'patient_reported', provenanceRef: 'ans-1',
      },
    ];
    demoTimeline.forEach(t => this.timelineEvents.set(t.id, t));

    // Seed a draft clinical summary for patient-1
    const demoSummary: ClinicalSummary = {
      id: 'summary-1', sessionId: 'session-1', patientId: 'patient-1',
      tenantId: 'hospital-1', encounterId: 'enc-1', status: 'draft',
      generatedAt: new Date(Date.now() - 1800000).toISOString(), version: 1,
      completenessScore: 0.85,
      sections: [
        {
          id: 'sec-cc', type: 'chief_complaint', title: 'Chief Complaint',
          content: 'Chest pain since this morning.',
          facts: ['fact-1'], reviewStatus: 'pending'
        },
        {
          id: 'sec-hpi', type: 'hpi', title: 'History of Present Illness',
          content: 'A 59-year-old male presents with left-sided crushing/pressing chest pain (severity 7/10) that started this morning. The pain radiates to the left arm. Patient is a known hypertensive on Amlodipine 5mg OD for approximately 5 years. He denies any history of diabetes mellitus, though a discharge summary from City Hospital dated 12 March 2023 documents Type 2 Diabetes Mellitus — this discrepancy requires clarification. He reports an allergy to sulfonamide antibiotics.',
          facts: ['fact-1', 'fact-2', 'fact-3', 'fact-4', 'fact-5', 'fact-7', 'fact-8', 'fact-11'],
          reviewStatus: 'pending'
        },
        {
          id: 'sec-pmh', type: 'past_medical_history', title: 'Past Medical History',
          content: '• Hypertension — approximately 5 years\n• Diabetes Mellitus — denied by patient; documented in City Hospital discharge summary (12 Mar 2023) [CONFLICTING]',
          facts: ['fact-6', 'fact-7', 'fact-9'], reviewStatus: 'pending'
        },
        {
          id: 'sec-meds', type: 'medications', title: 'Current Medications',
          content: '• Amlodipine 5mg once daily (oral) — patient-reported\n• Metformin 500mg twice daily (oral) — extracted from prescription, City Hospital (confidence: 72%) [requires verification]',
          facts: ['fact-8', 'fact-10'], reviewStatus: 'pending'
        },
        {
          id: 'sec-allergy', type: 'allergies', title: 'Allergies',
          content: '• Sulfonamide antibiotics — allergy (patient-reported)',
          facts: ['fact-11'], reviewStatus: 'pending'
        },
        {
          id: 'sec-fhx', type: 'family_history', title: 'Family History',
          content: 'Not assessed in this session.',
          facts: [], reviewStatus: 'pending'
        },
        {
          id: 'sec-ros', type: 'review_of_systems', title: 'Review of Systems',
          content: 'Not assessed in this session.',
          facts: [], reviewStatus: 'pending'
        },
      ],
      redFlags: demoAlerts,
      missingInformation: [
        { fieldName: 'family_history', fieldLabel: 'Family History', reason: 'not_asked', priority: 'recommended' },
        { fieldName: 'review_of_systems', fieldLabel: 'Review of Systems', reason: 'not_asked', priority: 'recommended' },
        { fieldName: 'personal_history', fieldLabel: 'Personal History (smoking, alcohol)', reason: 'not_asked', priority: 'recommended' },
        { fieldName: 'associated_symptoms', fieldLabel: 'Associated Symptoms (dyspnoea, diaphoresis, nausea)', reason: 'not_asked', priority: 'required' },
      ],
      conflicts: [
        {
          id: 'conflict-1', field: 'diabetes_mellitus', fieldLabel: 'Diabetes Mellitus',
          sources: [
            {
              value: 'Denies diabetes mellitus', source: 'Patient (today, self-report)',
              sourceType: 'patient_reported', provenanceRef: 'ans-6',
              timestamp: new Date(Date.now() - 3100000).toISOString()
            },
            {
              value: 'Type 2 Diabetes Mellitus', source: 'Discharge summary, City Hospital, 12 Mar 2023',
              sourceType: 'document_extracted', provenanceRef: 'doc-entity-1',
              timestamp: '2023-03-12T00:00:00Z'
            }
          ]
        }
      ]
    };
    this.summaries.set(demoSummary.id, demoSummary);

    // Seed encounters
    const demoEncounters: Encounter[] = [
      { id: 'enc-1', patientId: 'patient-1', tenantId: 'hospital-1', sessionId: 'session-1', department: 'General Medicine', status: 'ready', createdAt: new Date(Date.now() - 3600000).toISOString() },
      { id: 'enc-2', patientId: 'patient-2', tenantId: 'hospital-1', sessionId: 'session-2', department: 'General Medicine', status: 'intake', createdAt: new Date(Date.now() - 1200000).toISOString() },
      { id: 'enc-3', patientId: 'patient-3', tenantId: 'hospital-1', sessionId: 'session-3', department: 'General Medicine', status: 'intake', createdAt: new Date(Date.now() - 900000).toISOString() },
    ];
    demoEncounters.forEach(e => this.encounters.set(e.id, e));
  }
}

// Singleton
export const store = new DataStore();
