// ─── MediKiosk API Server ────────────────────────────────────────────────────

import express from 'express';
import cors from 'cors';
import helmet from 'helmet';
import { createServer } from 'http';
import { Server as SocketServer } from 'socket.io';
import { v4 as uuid } from 'uuid';

import { store } from './db/store.js';
import { questionEngine } from './engines/questionEngine.js';
import { redFlagEngine } from './engines/redFlagEngine.js';
import { timelineEngine } from './engines/timelineEngine.js';
import { summaryGenerator } from './engines/summaryGenerator.js';

import type {
  Patient, Session, Consent, ClinicalFact, Answer,
  RedFlagAlert, AuditEvent, ApiResponse
} from '@medikiosk/shared';

const app = express();
const server = createServer(app);
const io = new SocketServer(server, {
  cors: { origin: '*', methods: ['GET', 'POST'] }
});

app.use(cors());
app.use(helmet({ contentSecurityPolicy: false }));
app.use(express.json({ limit: '50mb' }));

// ─── Middleware: Audit Logger ────────────────────────────────────────────────

function logAudit(
  eventType: AuditEvent['eventType'],
  actorId: string,
  actorRole: string,
  resourceType: string,
  resourceId: string,
  details: Record<string, unknown> = {},
  patientId?: string,
  sessionId?: string,
  tenantId: string = 'hospital-1',
) {
  const prevHash = store.auditEvents.length > 0
    ? store.auditEvents[store.auditEvents.length - 1].hash
    : 'genesis';

  const event: AuditEvent = {
    id: uuid(),
    tenantId,
    eventType,
    timestamp: new Date().toISOString(),
    actorId,
    actorRole,
    patientId,
    sessionId,
    resourceType,
    resourceId,
    action: eventType,
    details,
    previousHash: prevHash,
    hash: uuid(), // In production, this would be a cryptographic hash
  };
  store.auditEvents.push(event);
}

// ─── Auth Routes ─────────────────────────────────────────────────────────────

app.post('/v1/auth/login', (req, res) => {
  const { email, role } = req.body;
  // Mock auth — find user by email or create a session token for the role
  const user = [...store.users.values()].find(u => u.email === email) ||
    [...store.users.values()].find(u => u.role === role);

  if (user) {
    res.json({
      success: true,
      data: {
        token: `mock-token-${uuid()}`,
        userId: user.id,
        role: user.role,
        tenantId: user.tenantId,
        name: user.name,
        expiresAt: new Date(Date.now() + 8 * 3600000).toISOString(),
      }
    });
  } else {
    res.json({
      success: true,
      data: {
        token: `mock-token-${uuid()}`,
        userId: 'user-guest',
        role: role || 'patient',
        tenantId: 'hospital-1',
        name: 'Guest User',
        expiresAt: new Date(Date.now() + 8 * 3600000).toISOString(),
      }
    });
  }
});

// ─── Patient Routes ──────────────────────────────────────────────────────────

app.post('/v1/patients/register', (req, res) => {
  const data = req.body;
  const existing = [...store.patients.values()].find(
    p => p.hospitalLocalId === data.hospitalLocalId && p.tenantId === 'hospital-1'
  );
  if (existing) {
    return res.json({ success: true, data: existing } as ApiResponse<Patient>);
  }

  const patient: Patient = {
    id: uuid(),
    tenantId: 'hospital-1',
    hospitalLocalId: data.hospitalLocalId || `MRN-${Date.now()}`,
    firstName: data.firstName,
    lastName: data.lastName,
    dateOfBirth: data.dateOfBirth,
    sex: data.sex,
    age: data.age,
    phone: data.phone,
    language: data.language || 'en',
    address: data.address,
    abhaId: data.abhaId,
    createdAt: new Date().toISOString(),
    updatedAt: new Date().toISOString(),
  };
  store.patients.set(patient.id, patient);
  logAudit('patient_registered', 'system', 'system', 'Patient', patient.id, {}, patient.id);

  res.json({ success: true, data: patient } as ApiResponse<Patient>);
});

app.get('/v1/patients', (_req, res) => {
  const patients = [...store.patients.values()];
  res.json({ success: true, data: patients, meta: { total: patients.length } });
});

app.get('/v1/patients/:id', (req, res) => {
  const patient = store.patients.get(req.params.id);
  if (!patient) return res.status(404).json({ success: false, error: { code: 'NOT_FOUND', message: 'Patient not found' } });
  res.json({ success: true, data: patient });
});

// ─── Session Routes ──────────────────────────────────────────────────────────

app.post('/v1/sessions', (req, res) => {
  const { patientId, protocolId, department, channel, language } = req.body;

  const encounter = {
    id: uuid(),
    patientId,
    tenantId: 'hospital-1',
    sessionId: '',
    department: department || 'General Medicine',
    status: 'intake' as const,
    createdAt: new Date().toISOString(),
  };

  const session: Session = {
    id: uuid(),
    patientId,
    tenantId: 'hospital-1',
    encounterId: encounter.id,
    protocolId: protocolId || 'general_medicine_v1',
    protocolVersion: '1.0',
    status: 'intake_active',
    language: language || 'en',
    channel: channel || 'kiosk',
    completenessScore: 0,
    startedAt: new Date().toISOString(),
    lastActivityAt: new Date().toISOString(),
    answeredQuestions: [],
  };

  encounter.sessionId = session.id;
  store.encounters.set(encounter.id, encounter);
  store.sessions.set(session.id, session);

  logAudit('session_started', 'system', 'system', 'Session', session.id, { channel, language }, patientId, session.id);

  // Get first question
  const answeredMap = new Map<string, unknown>();
  const nextQ = questionEngine.getNextQuestion([], answeredMap, language);

  res.json({ success: true, data: { session, encounter, nextQuestion: nextQ } });
});

app.get('/v1/sessions', (_req, res) => {
  const sessions = [...store.sessions.values()].map(s => {
    const patient = store.patients.get(s.patientId);
    const alerts = store.getAlertsBySession(s.id);
    return { ...s, patient, activeAlerts: alerts.filter(a => a.status === 'active' || a.status === 'escalated') };
  });
  res.json({ success: true, data: sessions });
});

app.get('/v1/sessions/:id', (req, res) => {
  const session = store.sessions.get(req.params.id);
  if (!session) return res.status(404).json({ success: false, error: { code: 'NOT_FOUND', message: 'Session not found' } });

  const patient = store.patients.get(session.patientId);
  const facts = store.getFactsBySession(session.id);
  const alerts = store.getAlertsBySession(session.id);

  res.json({ success: true, data: { session, patient, facts, alerts } });
});

// ─── Answer Submission ───────────────────────────────────────────────────────

app.post('/v1/sessions/:id/answers', (req, res) => {
  const session = store.sessions.get(req.params.id);
  if (!session) return res.status(404).json({ success: false, error: { code: 'NOT_FOUND', message: 'Session not found' } });

  const { questionId, value, inputMethod, voiceTranscript, idempotencyKey } = req.body;

  // Idempotency check
  if (idempotencyKey && store.checkIdempotency(idempotencyKey)) {
    const existingAnswer = [...store.answers.values()].find(a => a.questionId === questionId && a.sessionId === session.id);
    return res.json({ success: true, data: { answerId: existingAnswer?.id, duplicate: true } });
  }

  // Create answer
  const answer: Answer = {
    id: uuid(),
    sessionId: session.id,
    questionId,
    value,
    valueRaw: voiceTranscript || String(value),
    inputMethod: inputMethod || 'touch',
    confidence: 0.9,
    timestamp: new Date().toISOString(),
    confirmed: true,
  };
  store.answers.set(answer.id, answer);

  // Create clinical fact from answer
  const allQuestions = questionEngine.getAllQuestions();
  const question = allQuestions.find(q => q.id === questionId);

  if (question) {
    const fact: ClinicalFact = {
      id: uuid(),
      sessionId: session.id,
      category: question.category as ClinicalFact['category'],
      conceptCode: question.conceptCode,
      conceptLabel: question.conceptLabel,
      valueRaw: voiceTranscript || String(value),
      valueNormalized: normalizeValue(value, question),
      confidence: 0.9,
      source: 'patient_reported',
      provenanceRef: answer.id,
      isConflicting: false,
      timestamp: new Date().toISOString(),
    };
    store.clinicalFacts.set(fact.id, fact);
  }

  // Update session
  if (!session.answeredQuestions.includes(questionId)) {
    session.answeredQuestions.push(questionId);
  }
  session.lastActivityAt = new Date().toISOString();

  // Build answer map for engine
  const answers = store.getAnswersBySession(session.id);
  const answeredMap = questionEngine.buildAnswerMap(answers);

  // Evaluate red-flag rules AFTER every answer (PRD Section 9)
  const alreadyFiredRuleIds = new Set(
    store.getAlertsBySession(session.id).map(a => a.ruleId)
  );
  const { newAlerts } = redFlagEngine.evaluate(
    answeredMap, alreadyFiredRuleIds,
    session.id, session.patientId, session.tenantId, session.language
  );

  // Store new alerts and emit real-time notifications
  let redFlagFired = false;
  let alertResponse: { id: string; severity: string; patientMessage: string } | undefined;

  for (const alert of newAlerts) {
    store.redFlagAlerts.set(alert.id, alert);
    logAudit('red_flag_fired', 'system', 'system', 'RedFlagAlert', alert.id,
      { ruleId: alert.ruleId, severity: alert.severity, matchedConditions: alert.matchedConditions },
      session.patientId, session.id
    );

    // Emit to staff console via WebSocket
    io.emit('red-flag-alert', {
      alert,
      patient: store.patients.get(session.patientId),
      session,
    });

    redFlagFired = true;
    alertResponse = {
      id: alert.id,
      severity: alert.severity,
      patientMessage: redFlagEngine.getPatientMessage(alert.ruleId, session.language),
    };

    // Pause session on critical alerts
    if (alert.severity === 'critical') {
      session.status = 'intake_paused';
    }
  }

  // Update completeness score
  session.completenessScore = questionEngine.getCompletenessScore(
    session.answeredQuestions, answeredMap
  );

  store.sessions.set(session.id, session);

  // Get next question
  const nextQ = questionEngine.getNextQuestion(session.answeredQuestions, answeredMap, session.language);

  // If no more questions, mark session as complete
  if (!nextQ && !redFlagFired) {
    session.status = 'intake_complete';
    session.completedAt = new Date().toISOString();
    store.sessions.set(session.id, session);
    logAudit('session_completed', 'system', 'system', 'Session', session.id, { completenessScore: session.completenessScore }, session.patientId, session.id);
  }

  // Emit session update
  io.emit('session-update', { sessionId: session.id, status: session.status, completenessScore: session.completenessScore });

  res.json({
    success: true,
    data: {
      answerId: answer.id,
      redFlagFired,
      redFlagAlert: alertResponse,
      nextQuestion: nextQ,
      sessionStatus: session.status,
      completenessScore: session.completenessScore,
    }
  });
});

// Helper: normalize answer value to human-readable text
function normalizeValue(value: unknown, question: any): string {
  if (Array.isArray(value)) {
    const labels = value.map(v => {
      const opt = question.options?.find((o: any) => o.value === v);
      return opt?.label?.en || v;
    });
    return labels.join(', ');
  }
  if (typeof value === 'boolean') return value ? 'Yes' : 'No';
  if (typeof value === 'number' && question.widgetType === 'severity_slider') return `${value}/10`;
  return String(value);
}

// Attach the helper to `this` context for use in the route handler
// (In the actual route above, we use it as a standalone function)

// ─── Timeline Routes ─────────────────────────────────────────────────────────

app.get('/v1/sessions/:id/timeline', (req, res) => {
  const session = store.sessions.get(req.params.id);
  if (!session) return res.status(404).json({ success: false, error: { code: 'NOT_FOUND', message: 'Session not found' } });

  // Use pre-seeded timeline events or build new ones from facts
  let timeline = store.getTimelineBySession(session.id);
  if (timeline.length === 0) {
    const facts = store.getFactsBySession(session.id);
    timeline = timelineEngine.buildTimeline(facts, session.id);
    timeline.forEach(t => store.timelineEvents.set(t.id, t));
  }

  res.json({ success: true, data: timeline });
});

// ─── Red-Flag Routes ─────────────────────────────────────────────────────────

app.get('/v1/sessions/:id/red-flags', (req, res) => {
  const alerts = store.getAlertsBySession(req.params.id);
  res.json({ success: true, data: alerts });
});

app.get('/v1/red-flags/active', (_req, res) => {
  const alerts = store.getActiveAlerts();
  const enriched = alerts.map(a => ({
    ...a,
    patient: store.patients.get(a.patientId),
    session: store.sessions.get(a.sessionId),
  }));
  res.json({ success: true, data: enriched });
});

app.post('/v1/red-flags/:id/acknowledge', (req, res) => {
  const alert = store.redFlagAlerts.get(req.params.id);
  if (!alert) return res.status(404).json({ success: false, error: { code: 'NOT_FOUND', message: 'Alert not found' } });

  alert.status = 'acknowledged';
  alert.acknowledgedAt = new Date().toISOString();
  alert.acknowledgedBy = req.body.staffId || 'user-nurse-1';
  store.redFlagAlerts.set(alert.id, alert);

  logAudit('red_flag_acknowledged', alert.acknowledgedBy!, 'nurse', 'RedFlagAlert', alert.id, {}, alert.patientId, alert.sessionId);
  io.emit('red-flag-update', alert);

  res.json({ success: true, data: alert });
});

app.post('/v1/red-flags/:id/resolve', (req, res) => {
  const alert = store.redFlagAlerts.get(req.params.id);
  if (!alert) return res.status(404).json({ success: false, error: { code: 'NOT_FOUND', message: 'Alert not found' } });

  alert.status = req.body.falsePositive ? 'false_positive' : 'resolved';
  alert.resolvedAt = new Date().toISOString();
  store.redFlagAlerts.set(alert.id, alert);

  logAudit('red_flag_resolved', req.body.staffId || 'user-nurse-1', 'nurse', 'RedFlagAlert', alert.id, { falsePositive: req.body.falsePositive }, alert.patientId, alert.sessionId);
  io.emit('red-flag-update', alert);

  // Resume session if it was paused
  const session = store.sessions.get(alert.sessionId);
  if (session && session.status === 'intake_paused') {
    const remainingActive = store.getAlertsBySession(session.id)
      .filter(a => a.status === 'active' || a.status === 'escalated');
    if (remainingActive.length === 0) {
      session.status = 'intake_active';
      store.sessions.set(session.id, session);
      io.emit('session-update', { sessionId: session.id, status: session.status });
    }
  }

  res.json({ success: true, data: alert });
});

// ─── Summary Routes ──────────────────────────────────────────────────────────

app.get('/v1/sessions/:id/summary', (req, res) => {
  const session = store.sessions.get(req.params.id);
  if (!session) return res.status(404).json({ success: false, error: { code: 'NOT_FOUND', message: 'Session not found' } });

  let summary = store.getSummaryBySession(session.id);
  if (!summary) {
    // Generate on-demand
    const facts = store.getFactsBySession(session.id);
    const alerts = store.getAlertsBySession(session.id);
    const allQuestions = questionEngine.getAllQuestions();

    summary = summaryGenerator.generate(
      facts, alerts, session.id, session.patientId, session.tenantId, session.encounterId,
      session.answeredQuestions, allQuestions.filter(q => q.required).map(q => q.id)
    );

    store.summaries.set(summary.id, summary);
    logAudit('summary_generated', 'system', 'system', 'Summary', summary.id, {}, session.patientId, session.id);
  }

  // Enrich with patient info
  const patient = store.patients.get(session.patientId);
  res.json({ success: true, data: { summary, patient, session } });
});

app.post('/v1/summary/:id/review', (req, res) => {
  const summary = store.summaries.get(req.params.id);
  if (!summary) return res.status(404).json({ success: false, error: { code: 'NOT_FOUND', message: 'Summary not found' } });

  const { sectionId, action, editedContent, notes, physicianId } = req.body;

  if (sectionId) {
    // Per-section review
    const section = summary.sections.find(s => s.id === sectionId);
    if (section) {
      switch (action) {
        case 'accept':
          section.reviewStatus = 'accepted';
          break;
        case 'edit':
          section.reviewStatus = 'edited';
          section.editedContent = editedContent;
          section.physicianNotes = notes;
          break;
        case 'reject':
          section.reviewStatus = 'rejected';
          section.physicianNotes = notes;
          break;
      }
    }
  } else if (action === 'accept') {
    // Bulk approve all sections
    summary.sections.forEach(s => { s.reviewStatus = 'accepted'; });
    summary.status = 'approved';
    summary.approvedAt = new Date().toISOString();
    summary.approvedBy = physicianId || 'user-physician-1';

    // Update session status
    const session = store.sessions.get(summary.sessionId);
    if (session) {
      session.status = 'reviewed';
      store.sessions.set(session.id, session);
    }

    logAudit('summary_approved', physicianId || 'user-physician-1', 'physician', 'Summary', summary.id, {}, summary.patientId, summary.sessionId);
  }

  // Check if all sections are reviewed
  const allReviewed = summary.sections.every(s => s.reviewStatus !== 'pending');
  if (allReviewed && summary.status === 'draft') {
    summary.status = 'under_review';
  }

  store.summaries.set(summary.id, summary);
  io.emit('summary-update', { summaryId: summary.id, status: summary.status });

  res.json({ success: true, data: summary });
});

// ─── Consent Routes ──────────────────────────────────────────────────────────

app.post('/v1/consent', (req, res) => {
  const { sessionId, patientId, consents } = req.body;

  const recorded: Consent[] = [];
  for (const c of consents) {
    const consent: Consent = {
      id: uuid(),
      sessionId,
      patientId,
      tenantId: 'hospital-1',
      purpose: c.purpose,
      status: c.granted ? 'granted' : 'denied',
      grantedAt: c.granted ? new Date().toISOString() : undefined,
      explanation: {},
      version: '1.0',
    };
    store.consents.set(consent.id, consent);
    recorded.push(consent);

    logAudit(
      c.granted ? 'consent_granted' : 'consent_denied',
      'patient', 'patient', 'Consent', consent.id,
      { purpose: c.purpose },
      patientId, sessionId
    );
  }

  res.json({ success: true, data: recorded });
});

app.get('/v1/sessions/:id/consents', (req, res) => {
  const consents = store.getConsentsBySession(req.params.id);
  res.json({ success: true, data: consents });
});

// ─── Document Routes ─────────────────────────────────────────────────────────

app.post('/v1/documents', (req, res) => {
  const { sessionId, fileName, mimeType, fileSize } = req.body;
  const session = store.sessions.get(sessionId);
  if (!session) return res.status(404).json({ success: false, error: { code: 'NOT_FOUND', message: 'Session not found' } });

  const doc = {
    id: uuid(),
    sessionId,
    patientId: session.patientId,
    tenantId: session.tenantId,
    fileName: fileName || 'document.jpg',
    mimeType: mimeType || 'image/jpeg',
    fileSize: fileSize || 0,
    processingStage: 'uploaded' as const,
    uploadedAt: new Date().toISOString(),
    pageCount: 1,
    pages: [],
  };
  store.documents.set(doc.id, doc);

  logAudit('document_uploaded', 'patient', 'patient', 'Document', doc.id, { fileName: doc.fileName }, session.patientId, sessionId);

  // Simulate async processing pipeline
  simulateDocumentProcessing(doc.id);

  res.json({ success: true, data: { documentId: doc.id, processingStage: doc.processingStage, estimatedProcessingTime: 8 } });
});

app.get('/v1/documents/:id/status', (req, res) => {
  const doc = store.documents.get(req.params.id);
  if (!doc) return res.status(404).json({ success: false, error: { code: 'NOT_FOUND', message: 'Document not found' } });
  res.json({ success: true, data: doc });
});

async function simulateDocumentProcessing(docId: string) {
  const stages: Array<{ stage: string; delay: number }> = [
    { stage: 'quality_check', delay: 1000 },
    { stage: 'classifying', delay: 1500 },
    { stage: 'preprocessing', delay: 1000 },
    { stage: 'ocr_processing', delay: 2000 },
    { stage: 'entity_extraction', delay: 1500 },
    { stage: 'validation', delay: 1000 },
    { stage: 'completed', delay: 500 },
  ];

  for (const { stage, delay } of stages) {
    await new Promise(r => setTimeout(r, delay));
    const doc = store.documents.get(docId);
    if (doc) {
      doc.processingStage = stage as any;
      store.documents.set(docId, doc);
      io.emit('document-processing', { documentId: docId, stage });
    }
  }
}

// ─── Clinical Facts Routes ───────────────────────────────────────────────────

app.get('/v1/sessions/:id/facts', (req, res) => {
  const facts = store.getFactsBySession(req.params.id);
  res.json({ success: true, data: facts });
});

// ─── Audit Routes ────────────────────────────────────────────────────────────

app.get('/v1/audit', (req, res) => {
  const { eventType, patientId, sessionId, limit, offset } = req.query;
  const result = store.queryAuditEvents({
    tenantId: 'hospital-1',
    eventType: eventType as string,
    patientId: patientId as string,
    sessionId: sessionId as string,
    limit: limit ? parseInt(limit as string) : 50,
    offset: offset ? parseInt(offset as string) : 0,
  });
  res.json({ success: true, data: result.events, meta: { total: result.total } });
});

// ─── Admin Routes ────────────────────────────────────────────────────────────

app.get('/v1/admin/protocols', (_req, res) => {
  const protocols = store.getActiveProtocols();
  // Return the general_medicine_v1 protocol data as well
  res.json({
    success: true,
    data: [{
      id: 'general_medicine_v1',
      name: 'General Medicine Intake',
      version: '1.0',
      department: 'General Medicine',
      specialty: 'Internal Medicine',
      status: 'active',
      questionCount: questionEngine.getAllQuestions().length,
      groups: questionEngine.getGroupProgress([], new Map()),
    }]
  });
});

app.get('/v1/admin/rules', (_req, res) => {
  const rules = redFlagEngine.getRules();
  res.json({ success: true, data: rules });
});

app.get('/v1/admin/stats', (_req, res) => {
  const sessions = [...store.sessions.values()];
  const patients = [...store.patients.values()];
  const alerts = [...store.redFlagAlerts.values()];

  const stats = {
    totalPatients: patients.length,
    totalSessions: sessions.length,
    activeSessions: sessions.filter(s => s.status === 'intake_active' || s.status === 'intake_paused').length,
    completedSessions: sessions.filter(s => ['intake_complete', 'awaiting_review', 'under_review', 'reviewed'].includes(s.status)).length,
    awaitingReview: sessions.filter(s => s.status === 'awaiting_review').length,
    activeAlerts: alerts.filter(a => a.status === 'active' || a.status === 'escalated').length,
    resolvedAlerts: alerts.filter(a => a.status === 'resolved' || a.status === 'false_positive').length,
    avgCompleteness: sessions.length > 0
      ? sessions.reduce((sum, s) => sum + s.completenessScore, 0) / sessions.length
      : 0,
    sessionsByChannel: {
      kiosk: sessions.filter(s => s.channel === 'kiosk').length,
      tablet: sessions.filter(s => s.channel === 'tablet').length,
      phone: sessions.filter(s => s.channel === 'phone').length,
      web: sessions.filter(s => s.channel === 'web').length,
    },
    alertsBySeverity: {
      critical: alerts.filter(a => a.severity === 'critical').length,
      high: alerts.filter(a => a.severity === 'high').length,
      moderate: alerts.filter(a => a.severity === 'moderate').length,
    },
  };

  res.json({ success: true, data: stats });
});

// ─── Generate Summary on Demand ──────────────────────────────────────────────

app.post('/v1/sessions/:id/generate-summary', (req, res) => {
  const session = store.sessions.get(req.params.id);
  if (!session) return res.status(404).json({ success: false, error: { code: 'NOT_FOUND', message: 'Session not found' } });

  const facts = store.getFactsBySession(session.id);
  const alerts = store.getAlertsBySession(session.id);
  const allQuestions = questionEngine.getAllQuestions();

  const summary = summaryGenerator.generate(
    facts, alerts, session.id, session.patientId, session.tenantId, session.encounterId,
    session.answeredQuestions, allQuestions.filter(q => q.required).map(q => q.id)
  );

  store.summaries.set(summary.id, summary);
  session.status = 'awaiting_review';
  store.sessions.set(session.id, session);

  logAudit('summary_generated', 'system', 'system', 'Summary', summary.id, {}, session.patientId, session.id);

  res.json({ success: true, data: summary });
});

// ─── Next Question (standalone) ──────────────────────────────────────────────

app.get('/v1/sessions/:id/next-question', (req, res) => {
  const session = store.sessions.get(req.params.id);
  if (!session) return res.status(404).json({ success: false, error: { code: 'NOT_FOUND', message: 'Session not found' } });

  const answers = store.getAnswersBySession(session.id);
  const answeredMap = questionEngine.buildAnswerMap(answers);
  const nextQ = questionEngine.getNextQuestion(session.answeredQuestions, answeredMap, session.language);

  res.json({ success: true, data: nextQ });
});

// ─── Socket.IO Connection ────────────────────────────────────────────────────

io.on('connection', (socket) => {
  console.log(`[Socket.IO] Client connected: ${socket.id}`);

  socket.on('join-staff-room', () => {
    socket.join('staff');
    console.log(`[Socket.IO] ${socket.id} joined staff room`);
  });

  socket.on('join-physician-room', () => {
    socket.join('physician');
    console.log(`[Socket.IO] ${socket.id} joined physician room`);
  });

  socket.on('disconnect', () => {
    console.log(`[Socket.IO] Client disconnected: ${socket.id}`);
  });
});

// ─── Start Server ────────────────────────────────────────────────────────────

const PORT = process.env.PORT || 3001;
server.listen(PORT, () => {
  console.log(`
  ╔════════════════════════════════════════════════════════╗
  ║           MediKiosk API Server v1.0                   ║
  ║   AI-Powered Pre-Consultation Clinical Intake         ║
  ║                                                       ║
  ║   🌐 API:       http://localhost:${PORT}/v1             ║
  ║   🔌 WebSocket: ws://localhost:${PORT}                  ║
  ║   📊 Demo data: 3 patients, 3 sessions loaded        ║
  ║                                                       ║
  ║   Engines loaded:                                     ║
  ║   ✅ Question Engine (${questionEngine.getAllQuestions().length} questions)               ║
  ║   ✅ Red-Flag Engine (${redFlagEngine.getRules().length} rules)                    ║
  ║   ✅ Timeline Engine                                  ║
  ║   ✅ Conflict Detector                                ║
  ║   ✅ Summary Generator                                ║
  ╚════════════════════════════════════════════════════════╝
  `);
});
