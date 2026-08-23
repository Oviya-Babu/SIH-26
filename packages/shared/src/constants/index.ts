// ─── Constants ───────────────────────────────────────────────────────────────

export const SUPPORTED_LANGUAGES = {
  en: 'English',
  hi: 'हिन्दी',
  ta: 'தமிழ்',
  te: 'తెలుగు',
  bn: 'বাংলা',
  mr: 'मराठी',
  kn: 'ಕನ್ನಡ',
  gu: 'ગુજરાતી',
  ml: 'മലയാളം',
} as const;

export type LanguageCode = keyof typeof SUPPORTED_LANGUAGES;

export const SEVERITY_FACES = ['😊', '🙂', '😐', '😕', '😟', '😣', '😖', '😫', '😩', '🤯'] as const;

export const BODY_REGIONS = [
  'head', 'forehead', 'temple_left', 'temple_right', 'eyes', 'nose', 'ears',
  'mouth', 'throat', 'neck', 'neck_front', 'neck_back',
  'chest', 'chest_left', 'chest_right', 'chest_center',
  'upper_back', 'lower_back', 'spine',
  'abdomen', 'abdomen_upper', 'abdomen_lower', 'abdomen_left', 'abdomen_right',
  'shoulder_left', 'shoulder_right',
  'upper_arm_left', 'upper_arm_right', 'elbow_left', 'elbow_right',
  'forearm_left', 'forearm_right', 'wrist_left', 'wrist_right',
  'hand_left', 'hand_right',
  'hip_left', 'hip_right', 'groin',
  'thigh_left', 'thigh_right', 'knee_left', 'knee_right',
  'calf_left', 'calf_right', 'ankle_left', 'ankle_right',
  'foot_left', 'foot_right',
] as const;

export type BodyRegion = typeof BODY_REGIONS[number];

export const FREQUENCY_OPTIONS = [
  { value: 'once', label: { en: 'Once', hi: 'एक बार' } },
  { value: 'rarely', label: { en: 'Rarely', hi: 'कभी-कभी' } },
  { value: 'sometimes', label: { en: 'Sometimes', hi: 'कभी-कभी' } },
  { value: 'often', label: { en: 'Often', hi: 'अक्सर' } },
  { value: 'daily', label: { en: 'Daily', hi: 'रोज़' } },
  { value: 'constant', label: { en: 'Constant', hi: 'लगातार' } },
] as const;

export const DURATION_UNITS = [
  { value: 'minutes', label: { en: 'Minutes', hi: 'मिनट' } },
  { value: 'hours', label: { en: 'Hours', hi: 'घंटे' } },
  { value: 'days', label: { en: 'Days', hi: 'दिन' } },
  { value: 'weeks', label: { en: 'Weeks', hi: 'हफ्ते' } },
  { value: 'months', label: { en: 'Months', hi: 'महीने' } },
  { value: 'years', label: { en: 'Years', hi: 'साल' } },
] as const;

export const SESSION_TIMEOUT_MS = 15 * 60 * 1000; // 15 minutes idle timeout
export const CONFIDENCE_THRESHOLD_HIGH = 0.85;
export const CONFIDENCE_THRESHOLD_MEDIUM = 0.65;
export const CONFIDENCE_THRESHOLD_LOW = 0.4;
export const RED_FLAG_SLA_MINUTES = 5;
export const MAX_DOCUMENT_SIZE_MB = 20;
export const MAX_DOCUMENTS_PER_SESSION = 10;
