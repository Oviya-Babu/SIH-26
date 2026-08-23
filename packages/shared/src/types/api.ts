// ─── API Types ───────────────────────────────────────────────────────────────

/** Standard API response wrapper */
export interface ApiResponse<T> {
  success: boolean;
  data?: T;
  error?: ApiError;
  meta?: ApiMeta;
}

export interface ApiError {
  code: string;
  message: string;
  details?: Record<string, unknown>;
}

export interface ApiMeta {
  total?: number;
  limit?: number;
  offset?: number;
  page?: number;
}

/** User / Auth types */
export type UserRole =
  | 'patient'
  | 'nurse'
  | 'physician'
  | 'hospital_admin'
  | 'clinical_admin'
  | 'it_admin'
  | 'compliance_officer'
  | 'ayush_practitioner';

export interface User {
  id: string;
  tenantId: string;
  email: string;
  name: string;
  role: UserRole;
  department?: string;
  specialization?: string;
  active: boolean;
  createdAt: string;
}

export interface AuthToken {
  token: string;
  userId: string;
  role: UserRole;
  tenantId: string;
  expiresAt: string;
}

export interface LoginRequest {
  email: string;
  password: string;
  tenantId?: string;
}

/** Next question response from Question Engine */
export interface NextQuestionResponse {
  question: {
    id: string;
    conceptLabel: string;
    voicePrompt: string;
    touchLabel: string;
    widgetType: string;
    options?: { value: string; label: string; icon?: string }[];
    required: boolean;
    confirmBack: boolean;
    helpText?: string;
  };
  progress: {
    answeredCount: number;
    totalRequired: number;
    completenessScore: number;
    currentGroup: string;
    groupProgress: number;
  };
  sessionStatus: string;
}

/** Answer submission */
export interface SubmitAnswerRequest {
  questionId: string;
  value: unknown;
  inputMethod: 'voice' | 'touch' | 'text';
  voiceTranscript?: string;
  confirmed?: boolean;
  idempotencyKey: string;
}

export interface SubmitAnswerResponse {
  answerId: string;
  factId?: string;
  redFlagFired: boolean;
  redFlagAlert?: {
    id: string;
    severity: string;
    patientMessage: string;
  };
  nextQuestion?: NextQuestionResponse;
  confirmBack?: {
    questionId: string;
    interpretedValue: string;
    displayText: string;
  };
}

/** Integration types */
export interface FHIRExportResult {
  bundleId: string;
  resourceCount: number;
  status: 'success' | 'partial' | 'failed';
  errors?: string[];
  exportedAt: string;
}

export interface IntegrationSyncStatus {
  target: 'fhir' | 'abdm' | 'his';
  status: 'pending' | 'syncing' | 'synced' | 'failed' | 'queued';
  lastSyncAt?: string;
  lastError?: string;
  retryCount: number;
}
