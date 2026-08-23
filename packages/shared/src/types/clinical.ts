// ─── Clinical Types ──────────────────────────────────────────────────────────

/** Widget types for touch-first patient interaction */
export type QuestionWidgetType =
  | 'yes_no'
  | 'multi_select'
  | 'single_select'
  | 'body_map'
  | 'severity_slider'
  | 'duration_picker'
  | 'frequency_picker'
  | 'text_input'
  | 'number_input'
  | 'date_picker';

export type ClinicalCategory =
  | 'chief_complaint'
  | 'symptom'
  | 'past_medical_history'
  | 'past_surgical_history'
  | 'medication'
  | 'allergy'
  | 'family_history'
  | 'personal_history'
  | 'lifestyle'
  | 'investigation'
  | 'vital_sign'
  | 'ros' // review of systems
  | 'obstetric_history'
  | 'ayush_prakriti'
  | 'ayush_vikriti'
  | 'ayush_dashavidha';

export type FactSource = 'patient_reported' | 'document_extracted' | 'staff_entered' | 'system_derived';

/** A single clinical fact — the atomic unit of the Clinical Facts Store */
export interface ClinicalFact {
  id: string;
  sessionId: string;
  category: ClinicalCategory;
  conceptCode: string;
  conceptLabel: string;
  valueRaw: string;
  valueNormalized: string;
  valueStructured?: Record<string, unknown>;
  confidence: number;
  source: FactSource;
  provenanceRef: string; // links to Answer ID or ExtractedEntity ID
  isConflicting: boolean;
  conflictGroupId?: string;
  timestamp: string;
  metadata?: Record<string, unknown>;
}

/** A question node in the clinical ontology */
export interface QuestionNode {
  id: string;
  conceptCode: string;
  conceptLabel: string;
  category: ClinicalCategory;
  fieldName: string;
  dataType: 'boolean' | 'string' | 'number' | 'enum' | 'multi_enum' | 'date' | 'body_location';
  widgetType: QuestionWidgetType;
  required: boolean;
  options?: QuestionOption[];
  voicePrompt: Record<string, string>; // language code → prompt text
  touchLabel: Record<string, string>; // language code → label
  helpText?: Record<string, string>;
  dependsOn?: DependencyRule[];
  validationRules?: ValidationRule[];
  confirmBack?: boolean; // if true, confirm patient's answer before proceeding
  group: string; // question group for organization
  order: number;
}

export interface QuestionOption {
  value: string;
  label: Record<string, string>; // language code → display label
  icon?: string;
}

export interface DependencyRule {
  field: string;
  operator: 'equals' | 'not_equals' | 'in' | 'not_in' | 'gt' | 'lt' | 'exists';
  value: unknown;
}

export interface ValidationRule {
  type: 'min' | 'max' | 'pattern' | 'required_if';
  value: unknown;
  message: Record<string, string>;
}

/** An answer submitted by the patient */
export interface Answer {
  id: string;
  sessionId: string;
  questionId: string;
  value: unknown;
  valueRaw?: string; // original voice transcript or raw input
  inputMethod: 'voice' | 'touch' | 'text';
  confidence: number;
  timestamp: string;
  confirmed: boolean; // has patient confirmed via confirm-back
}

/** A versioned clinical protocol */
export interface Protocol {
  id: string;
  name: string;
  version: string;
  department: string;
  specialty: string;
  description: string;
  questionIds: string[];
  status: 'draft' | 'review' | 'approved' | 'active' | 'deprecated';
  createdBy: string;
  approvedBy?: string;
  createdAt: string;
  activatedAt?: string;
}

/** Symptom concept in the clinical ontology */
export interface SymptomConcept {
  code: string;
  label: Record<string, string>;
  category: string;
  requiredDimensions: string[]; // field names that must be asked
  optionalDimensions: string[];
  associatedSymptoms: string[]; // other symptom codes to ask about
  bodyLocations?: string[];
  keywords: Record<string, string[]>; // language code → trigger keywords
}
