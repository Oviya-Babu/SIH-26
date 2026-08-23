// ─── Audit Types ─────────────────────────────────────────────────────────────

export type AuditEventType =
  | 'patient_registered'
  | 'session_started'
  | 'session_completed'
  | 'session_abandoned'
  | 'consent_granted'
  | 'consent_denied'
  | 'consent_revoked'
  | 'answer_submitted'
  | 'document_uploaded'
  | 'document_processed'
  | 'red_flag_fired'
  | 'red_flag_acknowledged'
  | 'red_flag_escalated'
  | 'red_flag_resolved'
  | 'summary_generated'
  | 'summary_reviewed'
  | 'summary_approved'
  | 'summary_exported'
  | 'phi_accessed'
  | 'protocol_activated'
  | 'protocol_deactivated'
  | 'rule_updated'
  | 'integration_sync'
  | 'integration_failed';

export interface AuditEvent {
  id: string;
  tenantId: string;
  eventType: AuditEventType;
  timestamp: string;
  actorId: string;
  actorRole: string;
  patientId?: string;
  sessionId?: string;
  resourceType: string;
  resourceId: string;
  action: string;
  details: Record<string, unknown>;
  ipAddress?: string;
  deviceId?: string;
  previousHash?: string; // hash-chained for tamper evidence
  hash: string;
}

export interface AuditQueryParams {
  tenantId: string;
  eventType?: AuditEventType;
  actorId?: string;
  patientId?: string;
  sessionId?: string;
  startDate?: string;
  endDate?: string;
  limit?: number;
  offset?: number;
}
