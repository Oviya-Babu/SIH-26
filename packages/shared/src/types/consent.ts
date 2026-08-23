// ─── Consent Types ───────────────────────────────────────────────────────────

export type ConsentPurpose =
  | 'intake_processing'     // consent for intake session + AI processing
  | 'document_storage'      // consent for storing uploaded documents
  | 'abdm_sharing'          // consent for sharing via ABDM
  | 'his_integration'       // consent for pushing to hospital HIS
  | 'analytics'             // consent for anonymized analytics
  | 'research';             // consent for research use

export type ConsentStatus = 'granted' | 'denied' | 'revoked' | 'expired';

export interface Consent {
  id: string;
  sessionId: string;
  patientId: string;
  tenantId: string;
  purpose: ConsentPurpose;
  status: ConsentStatus;
  grantedAt?: string;
  revokedAt?: string;
  expiresAt?: string;
  explanation: Record<string, string>; // language → explanation shown to patient
  version: string;
}

export interface ConsentRequest {
  sessionId: string;
  patientId: string;
  consents: {
    purpose: ConsentPurpose;
    granted: boolean;
  }[];
}
