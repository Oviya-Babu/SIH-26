// ─── Summary Types ───────────────────────────────────────────────────────────

export type SummaryStatus = 'generating' | 'draft' | 'under_review' | 'approved' | 'exported';

export type ReviewAction = 'accept' | 'edit' | 'reject' | 'mark_incorrect' | 'request_clarification';

export interface ClinicalSummary {
  id: string;
  sessionId: string;
  patientId: string;
  tenantId: string;
  encounterId: string;
  status: SummaryStatus;
  generatedAt: string;
  approvedAt?: string;
  approvedBy?: string;
  sections: SummarySection[];
  redFlags: RedFlagAlert[];
  missingInformation: MissingInfo[];
  conflicts: ConflictInfo[];
  completenessScore: number;
  version: number;
}

export interface SummarySection {
  id: string;
  type: SummarySectionType;
  title: string;
  content: string; // generated prose
  facts: string[]; // IDs of ClinicalFacts cited
  reviewStatus: 'pending' | 'accepted' | 'edited' | 'rejected';
  physicianNotes?: string;
  editedContent?: string;
}

export type SummarySectionType =
  | 'chief_complaint'
  | 'hpi'
  | 'past_medical_history'
  | 'past_surgical_history'
  | 'medications'
  | 'allergies'
  | 'family_history'
  | 'personal_history'
  | 'review_of_systems'
  | 'investigations'
  | 'documents'
  | 'timeline';

export interface MissingInfo {
  fieldName: string;
  fieldLabel: string;
  reason: 'not_asked' | 'skipped' | 'dont_know' | 'unclear_response';
  priority: 'required' | 'recommended' | 'optional';
}

export interface ConflictInfo {
  id: string;
  field: string;
  fieldLabel: string;
  sources: ConflictSource[];
}

export interface ConflictSource {
  value: string;
  source: string; // e.g., "Patient (today, self-report)" or "Discharge summary, City Hospital, 12 Mar 2023"
  sourceType: 'patient_reported' | 'document_extracted';
  provenanceRef: string;
  timestamp: string;
}

export interface ReviewActionRequest {
  summaryId: string;
  sectionId?: string;
  action: ReviewAction;
  editedContent?: string;
  notes?: string;
  physicianId: string;
}

// ─── Red Flag Types ─────────────────────────────────────────────────────────

export type RedFlagSeverity = 'critical' | 'high' | 'moderate';

export type AlertStatus = 'active' | 'acknowledged' | 'escalated' | 'resolved' | 'false_positive';

export interface RedFlagRule {
  id: string;
  name: string;
  description: string;
  version: string;
  severity: RedFlagSeverity;
  conditions: RedFlagCondition[];
  logicOperator: 'AND' | 'OR';
  escalationMessage: Record<string, string>; // language → patient-facing calm message
  staffMessage: string;
  slaMinutes: number; // time before auto-escalation
  category: string;
  active: boolean;
}

export interface RedFlagCondition {
  field: string;
  operator: 'equals' | 'not_equals' | 'in' | 'gt' | 'lt' | 'contains' | 'exists';
  value: unknown;
}

export interface RedFlagAlert {
  id: string;
  sessionId: string;
  patientId: string;
  tenantId: string;
  ruleId: string;
  ruleName: string;
  severity: RedFlagSeverity;
  status: AlertStatus;
  matchedConditions: Record<string, unknown>;
  staffMessage: string;
  firedAt: string;
  acknowledgedAt?: string;
  acknowledgedBy?: string;
  resolvedAt?: string;
  escalatedAt?: string;
  escalationLevel: number;
}

// ─── Timeline Types ─────────────────────────────────────────────────────────

export interface TimelineEvent {
  id: string;
  sessionId: string;
  factId: string;
  date?: string;
  dateApproximate: boolean;
  dateLabel: string; // "12 Mar 2023" or "Approximately 2 years ago" or "Unknown"
  category: string;
  title: string;
  description: string;
  source: 'patient_reported' | 'document_extracted';
  provenanceRef: string;
  metadata?: Record<string, unknown>;
}
