// ─── MediKiosk API Client ────────────────────────────────────────────────────

const BASE_URL = '/api';

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const res = await fetch(`${BASE_URL}${path}`, {
    headers: {
      'Content-Type': 'application/json',
      ...options.headers,
    },
    ...options,
  });

  if (!res.ok) {
    const error = await res.json().catch(() => ({ message: 'Request failed' }));
    throw new Error(error.error?.message || error.message || 'Request failed');
  }

  return res.json();
}

export const api = {
  // Auth
  login: (email: string, role: string) =>
    request('/auth/login', { method: 'POST', body: JSON.stringify({ email, role }) }),

  // Patients
  getPatients: () => request<any>('/patients'),
  getPatient: (id: string) => request<any>(`/patients/${id}`),
  registerPatient: (data: any) =>
    request<any>('/patients/register', { method: 'POST', body: JSON.stringify(data) }),

  // Sessions
  createSession: (data: any) =>
    request<any>('/sessions', { method: 'POST', body: JSON.stringify(data) }),
  getSessions: () => request<any>('/sessions'),
  getSession: (id: string) => request<any>(`/sessions/${id}`),
  getNextQuestion: (sessionId: string) => request<any>(`/sessions/${sessionId}/next-question`),

  // Answers
  submitAnswer: (sessionId: string, data: any) =>
    request<any>(`/sessions/${sessionId}/answers`, { method: 'POST', body: JSON.stringify(data) }),

  // Timeline
  getTimeline: (sessionId: string) => request<any>(`/sessions/${sessionId}/timeline`),

  // Red Flags
  getSessionRedFlags: (sessionId: string) => request<any>(`/sessions/${sessionId}/red-flags`),
  getActiveAlerts: () => request<any>('/red-flags/active'),
  acknowledgeAlert: (alertId: string, staffId?: string) =>
    request<any>(`/red-flags/${alertId}/acknowledge`, { method: 'POST', body: JSON.stringify({ staffId }) }),
  resolveAlert: (alertId: string, falsePositive: boolean = false) =>
    request<any>(`/red-flags/${alertId}/resolve`, { method: 'POST', body: JSON.stringify({ falsePositive }) }),

  // Summary
  getSummary: (sessionId: string) => request<any>(`/sessions/${sessionId}/summary`),
  reviewSummary: (summaryId: string, data: any) =>
    request<any>(`/summary/${summaryId}/review`, { method: 'POST', body: JSON.stringify(data) }),
  generateSummary: (sessionId: string) =>
    request<any>(`/sessions/${sessionId}/generate-summary`, { method: 'POST' }),

  // Consent
  recordConsent: (data: any) =>
    request<any>('/consent', { method: 'POST', body: JSON.stringify(data) }),

  // Documents
  uploadDocument: (data: any) =>
    request<any>('/documents', { method: 'POST', body: JSON.stringify(data) }),
  getDocumentStatus: (docId: string) => request<any>(`/documents/${docId}/status`),

  // Facts
  getSessionFacts: (sessionId: string) => request<any>(`/sessions/${sessionId}/facts`),

  // Audit
  getAuditEvents: (params?: Record<string, string>) => {
    const query = params ? '?' + new URLSearchParams(params).toString() : '';
    return request<any>(`/audit${query}`);
  },

  // Admin
  getProtocols: () => request<any>('/admin/protocols'),
  getRules: () => request<any>('/admin/rules'),
  getStats: () => request<any>('/admin/stats'),
};
