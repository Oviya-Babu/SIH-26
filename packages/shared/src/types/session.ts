// ─── Session & Encounter Types ───────────────────────────────────────────────

export type SessionStatus =
  | 'language_selection'
  | 'consent'
  | 'intake_active'
  | 'intake_paused'
  | 'intake_complete'
  | 'document_upload'
  | 'awaiting_review'
  | 'under_review'
  | 'reviewed'
  | 'abandoned';

export interface Session {
  id: string;
  patientId: string;
  tenantId: string;
  encounterId: string;
  protocolId: string;
  protocolVersion: string;
  status: SessionStatus;
  language: string;
  channel: 'kiosk' | 'tablet' | 'phone' | 'web';
  completenessScore: number;
  startedAt: string;
  completedAt?: string;
  lastActivityAt: string;
  currentQuestionId?: string;
  answeredQuestions: string[];
  deviceId?: string;
}

export interface Encounter {
  id: string;
  patientId: string;
  tenantId: string;
  sessionId: string;
  department: string;
  status: 'intake' | 'ready' | 'in_consultation' | 'completed';
  scheduledAt?: string;
  createdAt: string;
}

export interface CreateSessionRequest {
  patientId: string;
  protocolId: string;
  department: string;
  channel: 'kiosk' | 'tablet' | 'phone' | 'web';
  language: string;
  deviceId?: string;
}
