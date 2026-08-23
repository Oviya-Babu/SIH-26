// ─── Document Types ──────────────────────────────────────────────────────────

export type DocumentType =
  | 'prescription'
  | 'discharge_summary'
  | 'lab_report'
  | 'radiology_report'
  | 'operative_note'
  | 'referral_letter'
  | 'insurance_card'
  | 'id_document'
  | 'other';

export type DocumentProcessingStage =
  | 'uploaded'
  | 'quality_check'
  | 'quality_rejected'
  | 'classifying'
  | 'preprocessing'
  | 'ocr_processing'
  | 'entity_extraction'
  | 'validation'
  | 'human_verification'
  | 'completed'
  | 'failed';

export interface MedicalDocument {
  id: string;
  sessionId: string;
  patientId: string;
  tenantId: string;
  fileName: string;
  mimeType: string;
  fileSize: number;
  documentType?: DocumentType;
  processingStage: DocumentProcessingStage;
  uploadedAt: string;
  processedAt?: string;
  pageCount: number;
  pages: DocumentPage[];
}

export interface DocumentPage {
  id: string;
  documentId: string;
  pageNumber: number;
  imageUrl: string;
  ocrText?: string;
  ocrConfidence?: number;
  entities: ExtractedEntity[];
}

export interface ExtractedEntity {
  id: string;
  pageId: string;
  documentId: string;
  entityType: 'medication' | 'diagnosis' | 'lab_value' | 'procedure' | 'vital' | 'date' | 'doctor_name' | 'hospital_name';
  valueRaw: string;
  valueNormalized?: string;
  confidence: number;
  boundingBox?: { x: number; y: number; width: number; height: number };
  modelVersion: string;
  extractionMethod: 'ocr' | 'handwriting' | 'manual';
  needsHumanVerification: boolean;
}

export interface DocumentUploadResponse {
  documentId: string;
  processingStage: DocumentProcessingStage;
  estimatedProcessingTime?: number;
}
