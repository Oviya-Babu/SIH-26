// ─── Patient Types ───────────────────────────────────────────────────────────

export interface Patient {
  id: string;
  abhaId?: string;
  hospitalLocalId: string;
  tenantId: string;
  firstName: string;
  lastName: string;
  dateOfBirth?: string;
  sex: 'male' | 'female' | 'other';
  age?: number;
  phone?: string;
  language: string;
  address?: string;
  createdAt: string;
  updatedAt: string;
}

export interface PatientRegistrationRequest {
  hospitalLocalId: string;
  firstName: string;
  lastName: string;
  dateOfBirth?: string;
  sex: 'male' | 'female' | 'other';
  age?: number;
  phone?: string;
  language: string;
  address?: string;
  abhaId?: string;
}
