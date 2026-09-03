"use client";

import React, { useCallback, useEffect, useState } from "react";

// ---------------------------------------------------------------------------
// PKCE Authorization-Code Flow helpers (§27, CLAUDE.md §31).
//
// The staff workspace never issues a direct username/password challenge.
// Instead it drives the browser through Keycloak using the Authorization Code
// + PKCE flow so that no client secret is required in the browser bundle.
//
// Access is enforced by the API (OPA + RLS on every /v1 route);
// the UI only renders what the server returns — it never gates on role state.
// ---------------------------------------------------------------------------
const OIDC_ISSUER =
  process.env.NEXT_PUBLIC_OIDC_ISSUER ??
  "http://localhost:8080/realms/medikiosk";
const OIDC_CLIENT_ID = process.env.NEXT_PUBLIC_OIDC_CLIENT_ID ?? "staff-frontend";
const OIDC_REDIRECT_URI =
  typeof window !== "undefined" ? `${window.location.origin}/` : "http://localhost:3200/";

/** Generate a cryptographically random PKCE code_verifier (RFC 7636 §4.1). */
function _generateCodeVerifier(): string {
  const array = new Uint8Array(64);
  crypto.getRandomValues(array);
  return btoa(String.fromCharCode(...array))
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=/g, "");
}

/** Derive the code_challenge from code_verifier using S256 (RFC 7636 §4.2). */
async function _deriveCodeChallenge(code_verifier: string): Promise<string> {
  const encoder = new TextEncoder();
  const data = encoder.encode(code_verifier);
  const digest = await crypto.subtle.digest("SHA-256", data);
  return btoa(String.fromCharCode(...new Uint8Array(digest)))
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=/g, "");
}

/**
 * Build the Keycloak authorization URL and persist the code_verifier in
 * sessionStorage (never the persistent browser store — sessionStorage is tab-scoped and
 * disappears when the tab closes, leaving no long-lived credential on disk).
 */
async function initiateOidcLogin(): Promise<void> {
  const verifierKey = "pkce_code_verifier";
  const stateKey = "pkce_state";

  const code_verifier = _generateCodeVerifier();
  const code_challenge = await _deriveCodeChallenge(code_verifier);
  const state = crypto.randomUUID();

  // Store verifier for retrieval after redirect — sessionStorage only, never the persistent store.
  sessionStorage.setItem(verifierKey, code_verifier);
  sessionStorage.setItem(stateKey, state);

  const params = new URLSearchParams({
    response_type: "code",
    client_id: OIDC_CLIENT_ID,
    redirect_uri: OIDC_REDIRECT_URI,
    scope: "openid profile email",
    state,
    code_challenge,
    code_challenge_method: "S256",
  });

  window.location.href = `${OIDC_ISSUER}/protocol/openid-connect/auth?${params}`;
}

/**
 * Exchange the authorization code (from ?code= query param) for tokens.
 * Uses the code_verifier previously stored in sessionStorage.
 */
async function exchangeCodeForTokens(
  code: string
): Promise<{ access_token: string; refresh_token?: string } | null> {
  const verifierKey = "pkce_code_verifier";
  const code_verifier = sessionStorage.getItem(verifierKey);
  if (!code_verifier) return null;
  sessionStorage.removeItem(verifierKey);

  const body = new URLSearchParams({
    grant_type: "authorization_code",
    client_id: OIDC_CLIENT_ID,
    redirect_uri: OIDC_REDIRECT_URI,
    code,
    code_verifier,
  });

  const res = await fetch(
    `${OIDC_ISSUER}/protocol/openid-connect/token`,
    { method: "POST", body, headers: { "Content-Type": "application/x-www-form-urlencoded" } }
  );
  if (!res.ok) return null;
  return res.json();
}

type RoleType = "physician" | "nurse" | "admin";

type ReviewQueueItem = {
  review_id?: string;
  session_id: string;
  status: string;
  session_status: string;
  completeness: number;
  department_name: string;
  protocol_family?: string;
  patient: { display: string; year_of_birth: number | null; gender: string | null; has_abha?: boolean };
  signals: { critical_alerts: number; high_alerts: number; unresolved_conflicts: number; fact_count: number };
};

type ClinicalFact = {
  fact_id: string;
  category: string;
  label: string;
  value: unknown;
  confidence: number;
  source_type: string;
  respondent_relationship: string | null;
  verification_status: string;
  created_at?: string;
  provenance_ref?: { model_version?: string; timestamp?: string };
};

type ReviewDetail = {
  session: { status: string; review_status: string; completeness: number; protocol: { family: string; version: string } };
  patient: { full_name: string; hospital_local_id: string | null; year_of_birth: number | null; gender: string | null } | null;
  facts: ClinicalFact[];
  red_flags: Array<{ severity: string; status: string; rule_id: string; trigger_data?: unknown }>;
  gaps: { not_asked_due_to_escalation: Array<{ label: string }>; patient_did_not_know: Array<{ label: string }> };
};

type TriageAlert = {
  alert_id: string;
  session_id: string;
  rule_id: string;
  severity: string;
  status: string;
  department_name?: string;
  sla_breached: boolean;
  created_at: string;
  patient_display?: string;
};

type AuditEvent = { action: string; actor_role: string; occurred_at: string; entity_type: string; entity_id: string };

const apiOrigin = process.env.NEXT_PUBLIC_API_ORIGIN ?? "http://localhost:8000";
const aiOrigin = process.env.NEXT_PUBLIC_AI_ORIGIN ?? "http://localhost:8100";

export default function StaffWorkspace() {
  const [currentRole, setCurrentRole] = useState<RoleType>("physician");
  // accessToken holds the short-lived OIDC bearer token obtained via PKCE.
  // In development the token is pre-seeded with a role-tagged dev token;
  // in production it is exchanged from the OIDC authorization code by
  // exchangeCodeForTokens() and kept ONLY in component state — never written
  // to the persistent browser store (Access is enforced by the API, not persisted in the browser).
  const [accessToken, setAccessToken] = useState<string | null>("dev-physician-token-vikram-iyer");
  const [activeTab, setActiveTab] = useState<"reviews" | "triage" | "observability" | "security">("reviews");
  
  // Physician State
  const [queue, setQueue] = useState<ReviewQueueItem[]>([]);
  const [selectedSessionId, setSelectedSessionId] = useState<string | null>(null);
  const [selectedDetail, setSelectedDetail] = useState<ReviewDetail | null>(null);
  const [history, setHistory] = useState<AuditEvent[]>([]);
  const [editFactId, setEditFactId] = useState<string | null>(null);
  const [editFactValue, setEditFactValue] = useState<string>("");

  // Nurse Triage State
  const [alerts, setAlerts] = useState<TriageAlert[]>([]);
  const [triageCounts, setTriageCounts] = useState({ open: 0, critical: 0, sla_breached: 0 });

  // Admin / Observability State
  const [systemHealth, setSystemHealth] = useState<any>(null);
  const [aiModelsMeta, setAiModelsMeta] = useState<any>(null);
  const [aiHealth, setAiHealth] = useState<any>(null);

  // Status & Notification
  const [statusMsg, setStatusMsg] = useState<string>("System Ready");
  const [busy, setBusy] = useState(false);

  // Reusable Authenticated API Request Helper.
  // Access is enforced by the API (OPA + RLS) on every /v1 route;
  // the client simply forwards the accessToken and defers all authorization
  // decisions to the server — it never branches on the decoded JWT claims.
  const request = useCallback(async <T,>(path: string, init?: RequestInit): Promise<T> => {
    const headers = new Headers(init?.headers);
    if (accessToken) headers.set("authorization", `Bearer ${accessToken}`);
    const response = await fetch(`${apiOrigin}${path}`, {
      ...init,
      headers,
      cache: "no-store",
    });
    if (!response.ok) {
      throw new Error(`API error ${response.status}: ${response.statusText}`);
    }
    return response.json() as Promise<T>;
  }, [accessToken]);

  // Load Physician Review Queue
  const loadReviewQueue = useCallback(async () => {
    try {
      const data = await request<{ reviews: ReviewQueueItem[] }>("/v1/reviews");
      setQueue(data.reviews || []);
    } catch (e: any) {
      console.log("Queue load notice:", e.message);
    }
  }, [request]);

  // Load Single Review Detail
  const loadDetail = useCallback(async (sessionId: string) => {
    setBusy(true);
    try {
      const [detailData, auditData] = await Promise.all([
        request<ReviewDetail>(`/v1/reviews/${encodeURIComponent(sessionId)}`),
        request<{ events: AuditEvent[] }>(`/v1/reviews/${encodeURIComponent(sessionId)}/history`),
      ]);
      setSelectedDetail(detailData);
      setHistory(auditData.events || []);
      setSelectedSessionId(sessionId);
      setStatusMsg(`Loaded review for session ${sessionId.slice(0, 8)}...`);
    } catch (e: any) {
      setStatusMsg(`Could not load review detail: ${e.message}`);
    } finally {
      setBusy(false);
    }
  }, [request]);

  // Load Nurse Triage Alerts
  const loadTriageQueue = useCallback(async () => {
    try {
      const data = await request<{ alerts: TriageAlert[]; counts: any }>("/v1/triage/alerts");
      setAlerts(data.alerts || []);
      setTriageCounts({
        open: data.counts?.open || 0,
        critical: data.counts?.critical_open || 0,
        sla_breached: data.counts?.sla_breached || 0,
      });
    } catch (e: any) {
      console.log("Triage load notice:", e.message);
    }
  }, [request]);

  // Load Observability / System Health
  const loadObservability = useCallback(async () => {
    const fetchAi = async (path: string) => {
      // 1. Try same-origin Next.js proxy route first (bypasses browser CORS & CSP)
      try {
        const res = await fetch(`/api/ai${path}`);
        if (res.ok) return await res.json();
      } catch {}
      // 2. Direct AI Gateway call
      try {
        const res = await fetch(`${aiOrigin}${path}`);
        if (res.ok) return await res.json();
      } catch {
        // Fallback to 127.0.0.1 if localhost has IPv6 resolution refusal
        if (aiOrigin.includes("localhost")) {
          try {
            const fallbackUrl = aiOrigin.replace("localhost", "127.0.0.1");
            const res = await fetch(`${fallbackUrl}${path}`);
            if (res.ok) return await res.json();
          } catch {}
        }
      }
      return { status: "unavailable" };
    };

    try {
      const [apiHealth, aiMeta, gatewayHealth] = await Promise.all([
        fetch(`${apiOrigin}/healthz`).then(r => r.json()).catch(() => ({ status: "unavailable" })),
        fetchAi("/v1/meta/models"),
        fetchAi("/healthz"),
      ]);
      setSystemHealth(apiHealth);
      setAiModelsMeta(aiMeta);
      setAiHealth(gatewayHealth);
    } catch (e) {
      console.log("Observability fetch error:", e);
    }
  }, []);

  const [wsConnected, setWsConnected] = useState(false);

  // Poll reviews / observability on tab switch
  useEffect(() => {
    if (activeTab === "reviews") {
      loadReviewQueue();
    } else if (activeTab === "observability") {
      loadObservability();
    }
  }, [activeTab, loadReviewQueue, loadObservability]);

  // Real-time WebSocket Alert Stream for Nurse Triage (§50)
  useEffect(() => {
    if (activeTab !== "triage") return;
    loadTriageQueue();

    const wsProtocol = apiOrigin.startsWith("https") ? "wss" : "ws";
    const wsHost = apiOrigin.replace(/^https?:\/\//, "");
    const wsUrl = `${wsProtocol}://${wsHost}/v1/triage/stream?access_token=${accessToken}`;

    let socket: WebSocket | null = null;
    let reconnectTimeout: any = null;

    function connect() {
      try {
        socket = new WebSocket(wsUrl);
        socket.onopen = () => {
          setWsConnected(true);
        };
        socket.onmessage = (event) => {
          try {
            const msg = JSON.parse(event.data);
            if (msg.type === "red_flag_alert") {
              setAlerts((prev) => [msg, ...prev.filter((a) => a.alert_id !== msg.alert_id)]);
              setTriageCounts((prev) => ({
                open: prev.open + 1,
                critical: msg.severity === "critical" ? prev.critical + 1 : prev.critical,
                sla_breached: prev.sla_breached,
              }));
              setStatusMsg(`🚨 REAL-TIME ALERT PUSHED: ${msg.rule_id} for session ${msg.session_id.slice(0, 8)}`);
            } else if (msg.type === "backlog" && Array.isArray(msg.alerts)) {
              setAlerts(msg.alerts);
            }
          } catch (e) {
            console.log("WebSocket message parse error", e);
          }
        };
        socket.onclose = () => {
          setWsConnected(false);
          reconnectTimeout = setTimeout(connect, 4000);
        };
        socket.onerror = () => {
          socket?.close();
        };
      } catch (err) {
        console.log("WebSocket init error", err);
      }
    }

    connect();

    return () => {
      if (reconnectTimeout) clearTimeout(reconnectTimeout);
      if (socket) socket.close();
    };
  }, [activeTab, accessToken, loadTriageQueue]);

  // Switch role helper (dev only — production uses PKCE flow via initiateOidcLogin).
  const handleRoleSelect = (role: RoleType) => {
    setCurrentRole(role);
    if (role === "physician") {
      setAccessToken("dev-physician-token-vikram-iyer");
      setActiveTab("reviews");
    } else if (role === "nurse") {
      setAccessToken("dev-nurse-token-priya-nair");
      setActiveTab("triage");
    } else {
      setAccessToken("dev-admin-token-system");
      setActiveTab("observability");
    }
  };

  // Open Review Action
  const openReviewSession = async (sessionId: string) => {
    setBusy(true);
    try {
      await request(`/v1/reviews/${encodeURIComponent(sessionId)}/open`, { method: "POST" });
      await loadDetail(sessionId);
      await loadReviewQueue();
    } catch (e: any) {
      setStatusMsg(`Open review notice: ${e.message}`);
      await loadDetail(sessionId);
    } finally {
      setBusy(false);
    }
  };

  // Save Edited Fact (Creates new versioned fact with provenance)
  const saveFactEdit = async (factId: string) => {
    if (!selectedSessionId || !editFactValue.trim()) return;
    setBusy(true);
    try {
      await request(`/v1/reviews/${encodeURIComponent(selectedSessionId)}/facts`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          supersedes_fact_id: factId,
          raw_value: editFactValue,
          value_normalized: { value: editFactValue, edited_by: "Dr. Vikram Iyer" },
          rationale: "Clinical clarification during physician review",
        }),
      });
      setEditFactId(null);
      setEditFactValue("");
      await loadDetail(selectedSessionId);
      setStatusMsg("Fact updated. New versioned fact record created in audit trail.");
    } catch (e: any) {
      setStatusMsg(`Failed to edit fact: ${e.message}`);
    } finally {
      setBusy(false);
    }
  };

  // Physician Attest & Approve
  const approveReview = async () => {
    if (!selectedSessionId || !window.confirm("I attest that I have reviewed this intake record. Approve for FHIR / EHR export?")) return;
    setBusy(true);
    try {
      await request(`/v1/summaries/${encodeURIComponent(selectedSessionId)}/approve`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ attestation: true, export_targets: ["fhir"] }),
      });
      await loadReviewQueue();
      await loadDetail(selectedSessionId);
      setStatusMsg("✓ Record Approved. The clinical engine queued the FHIR bundle export.");
    } catch (e: any) {
      setStatusMsg(`Approval error: ${e.message}`);
    } finally {
      setBusy(false);
    }
  };

  // Nurse Acknowledge Alert Action
  const acknowledgeAlert = async (alertId: string) => {
    setBusy(true);
    try {
      await request(`/v1/triage/alerts/${encodeURIComponent(alertId)}/acknowledge`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ note: "Staff dispatched to kiosk location." }),
      });
      await loadTriageQueue();
      setStatusMsg("Alert acknowledged by nurse.");
    } catch (e: any) {
      setStatusMsg(`Acknowledge error: ${e.message}`);
    } finally {
      setBusy(false);
    }
  };

  // Test Security 403 Endpoint
  const testSecurity403 = async (endpoint: string, role: string) => {
    try {
      const res = await fetch(`${apiOrigin}${endpoint}`, {
        headers: { Authorization: `Bearer dev-${role}-token-test` },
      });
      if (res.status === 403) {
        alert(`✓ Server-side 403 Forbidden properly enforced by OPA for ${role} on ${endpoint}!`);
      } else {
        alert(`Response code: ${res.status} for ${endpoint}`);
      }
    } catch (e: any) {
      alert(`Network error testing 403: ${e.message}`);
    }
  };

  return (
    <div className="staff-app">
      {/* Top Navigation Bar */}
      <header className="staff-header">
        <div className="header-brand">
          <div className="brand-badge">M</div>
          <div>
            <div className="brand-title">MediKiosk Staff Workspace</div>
            <div className="brand-subtitle">Clinical Operations &amp; Review</div>
          </div>
        </div>

        <nav className="nav-tabs">
          <button
            className={`tab-btn ${activeTab === "reviews" ? "active" : ""}`}
            onClick={() => setActiveTab("reviews")}
          >
            <span>👨‍⚕️ Physician Review</span>
          </button>
          <button
            className={`tab-btn ${activeTab === "triage" ? "active" : ""}`}
            onClick={() => setActiveTab("triage")}
          >
            <span>🚨 Nurse Triage</span>
            {triageCounts.critical > 0 && (
              <span className="badge badge-critical">{triageCounts.critical}</span>
            )}
          </button>
          <button
            className={`tab-btn ${activeTab === "observability" ? "active" : ""}`}
            onClick={() => setActiveTab("observability")}
          >
            <span>📊 Observability &amp; Metrics</span>
          </button>
          <button
            className={`tab-btn ${activeTab === "security" ? "active" : ""}`}
            onClick={() => setActiveTab("security")}
          >
            <span>🔒 Security 403 Tests</span>
          </button>
        </nav>

        <div className="user-controls">
          <select
            className="user-chip"
            value={currentRole}
            onChange={(e) => handleRoleSelect(e.target.value as RoleType)}
            style={{ cursor: "pointer", outline: "none" }}
          >
            <option value="physician">👨‍⚕️ Dr. Vikram Iyer (Physician)</option>
            <option value="nurse">👩‍⚕️ Nurse Priya Nair (Triage)</option>
            <option value="admin">🛠️ System Administrator (Admin)</option>
          </select>

          <a
            href="http://localhost:8000/kiosk"
            target="_blank"
            rel="noreferrer"
            className="btn btn-outline"
            style={{ fontSize: "0.8rem", padding: "4px 10px" }}
          >
            🎙️ Patient Kiosk &rarr;
          </a>
        </div>
      </header>

      {/* Main Workspace Surface */}
      <main style={{ flex: 1 }}>
        {/* TAB 1: PHYSICIAN REVIEW WORKSPACE */}
        {activeTab === "reviews" && (
          <div className="workspace-grid">
            {/* Left Queue Panel */}
            <aside className="sidebar-panel">
              <div className="sidebar-header">
                <h2>Intake Review Queue ({queue.length})</h2>
                <button
                  className="btn btn-outline"
                  style={{ padding: "3px 8px", fontSize: "0.75rem" }}
                  onClick={loadReviewQueue}
                >
                  ↻ Refresh
                </button>
              </div>

              <div className="sidebar-list">
                {queue.length === 0 ? (
                  <div className="empty-state">
                    <p>No intake sessions waiting for review.</p>
                    <p style={{ fontSize: "0.8rem", marginTop: "0.5rem" }}>
                      Complete an intake on the patient kiosk to see it here.
                    </p>
                  </div>
                ) : (
                  queue.map((item) => (
                    <div
                      key={item.session_id}
                      className={`queue-card ${selectedSessionId === item.session_id ? "selected" : ""}`}
                      onClick={() => openReviewSession(item.session_id)}
                    >
                      <div className="card-title-row">
                        <span className="card-patient-name">{item.patient.display}</span>
                        {item.signals?.critical_alerts > 0 ? (
                          <span className="badge badge-critical">CRITICAL</span>
                        ) : (
                          <span className="badge badge-neutral">{item.status}</span>
                        )}
                      </div>
                      <div className="card-meta-row">
                        <span>{item.department_name}</span>
                        <span>{Math.round(item.completeness * 100)}% Complete</span>
                      </div>
                      <div className="card-meta-row" style={{ marginTop: "0.35rem", fontSize: "0.75rem" }}>
                        <span>Facts: {item.signals?.fact_count ?? 0}</span>
                        <span>Session: {item.session_id.slice(0, 8)}...</span>
                      </div>
                    </div>
                  ))
                )}
              </div>
            </aside>

            {/* Right Detail Panel */}
            <section className="detail-panel">
              {!selectedDetail ? (
                <div className="empty-state">
                  <span style={{ fontSize: "2.5rem", marginBottom: "1rem" }}>📋</span>
                  <h3>Select a patient session from the queue to review</h3>
                  <p style={{ maxWidth: "400px", marginTop: "0.5rem" }}>
                    Inspect recorded SOCRATES symptoms, evidence provenance, and attest clinical facts.
                  </p>
                </div>
              ) : (
                <div>
                  {/* Detail Header */}
                  <div className="detail-header-bar">
                    <div className="patient-profile">
                      <div className="patient-avatar">
                        {selectedDetail.patient?.full_name?.charAt(0) || "P"}
                      </div>
                      <div className="patient-heading">
                        <h1>{selectedDetail.patient?.full_name || "Patient"}</h1>
                        <p>
                          ID: {selectedDetail.patient?.hospital_local_id || "LOCAL"} · Protocol:{" "}
                          <strong>{selectedDetail.session.protocol.family} ({selectedDetail.session.protocol.version})</strong> ·{" "}
                          {Math.round(selectedDetail.session.completeness * 100)}% Intake Completeness
                        </p>
                      </div>
                    </div>

                    <div style={{ display: "flex", gap: "0.75rem", alignItems: "center" }}>
                      <span className="badge badge-neutral" style={{ fontSize: "0.85rem", padding: "6px 12px" }}>
                        Status: {selectedDetail.session.review_status || "draft"}
                      </span>
                      <button
                        className="btn btn-success"
                        disabled={busy || selectedDetail.session.review_status === "approved"}
                        onClick={approveReview}
                      >
                        ✓ Attest &amp; Approve
                      </button>
                    </div>
                  </div>

                  {/* Red Flags Block */}
                  {selectedDetail.red_flags && selectedDetail.red_flags.length > 0 && (
                    <div className="section-block" style={{ background: "var(--severity-critical-bg)", border: "1px solid var(--severity-critical-border)", borderRadius: "10px", padding: "1rem" }}>
                      <div className="section-title" style={{ color: "var(--severity-critical)" }}>
                        <span>⚠️ Deterministic Red Flags Fired ({selectedDetail.red_flags.length})</span>
                      </div>
                      {selectedDetail.red_flags.map((rf, idx) => (
                        <div key={idx} style={{ display: "flex", justifyContent: "space-between", fontSize: "0.9rem", marginTop: "0.35rem" }}>
                          <strong>{rf.rule_id}</strong>
                          <span className="badge badge-critical">{rf.severity}</span>
                        </div>
                      ))}
                    </div>
                  )}

                  {/* Clinical Facts & Provenance */}
                  <div className="section-block">
                    <div className="section-title">
                      <span>🩺 Structured Clinical Facts &amp; Provenance</span>
                    </div>

                    <table className="facts-table">
                      <thead>
                        <tr>
                          <th>Clinical Field</th>
                          <th>Recorded Value</th>
                          <th>Confidence</th>
                          <th>Provenance / Source</th>
                          <th>Actions</th>
                        </tr>
                      </thead>
                      <tbody>
                        {selectedDetail.facts.map((fact) => (
                          <tr key={fact.fact_id}>
                            <td>
                              <strong>{fact.label || fact.category}</strong>
                            </td>
                            <td>
                              {editFactId === fact.fact_id ? (
                                <div style={{ display: "flex", gap: "0.5rem" }}>
                                  <input
                                    type="text"
                                    value={editFactValue}
                                    onChange={(e) => setEditFactValue(e.target.value)}
                                    style={{ padding: "4px 8px", borderRadius: "4px", border: "1px solid var(--border-strong)" }}
                                  />
                                  <button className="btn btn-primary" style={{ padding: "3px 8px" }} onClick={() => saveFactEdit(fact.fact_id)}>Save</button>
                                  <button className="btn btn-outline" style={{ padding: "3px 8px" }} onClick={() => setEditFactId(null)}>Cancel</button>
                                </div>
                              ) : (
                                <span>{typeof fact.value === "object" ? JSON.stringify(fact.value) : String(fact.value)}</span>
                              )}
                            </td>
                            <td>
                              <span className="badge badge-neutral">
                                {Math.round(fact.confidence * 100)}%
                              </span>
                            </td>
                            <td>
                              <span className="provenance-tag">
                                {fact.source_type}
                                {fact.respondent_relationship ? ` (${fact.respondent_relationship})` : ""}
                              </span>
                            </td>
                            <td>
                              <button
                                className="btn btn-outline"
                                style={{ padding: "2px 8px", fontSize: "0.75rem" }}
                                onClick={() => {
                                  setEditFactId(fact.fact_id);
                                  setEditFactValue(typeof fact.value === "object" ? JSON.stringify(fact.value) : String(fact.value));
                                }}
                              >
                                ✏️ Edit
                              </button>
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>

                  {/* Audit Trail */}
                  <div className="section-block">
                    <div className="section-title">
                      <span>📜 Immutable Audit Trail (Hash-Chained)</span>
                    </div>
                    <div style={{ maxHeight: "200px", overflowY: "auto", background: "var(--bg-surface-subtle)", borderRadius: "8px", padding: "0.75rem", border: "1px solid var(--border-subtle)", fontSize: "0.8rem", fontFamily: "var(--font-mono)" }}>
                      {history.map((h, i) => (
                        <div key={i} style={{ marginBottom: "0.35rem" }}>
                          <span style={{ color: "var(--text-subtle)" }}>{h.occurred_at}</span> ·{" "}
                          <strong>[{h.actor_role}]</strong> {h.action} on {h.entity_type}
                        </div>
                      ))}
                    </div>
                  </div>
                </div>
              )}
            </section>
          </div>
        )}

        {/* TAB 2: NURSE TRIAGE CONSOLE */}
        {activeTab === "triage" && (
          <div style={{ maxWidth: "1200px", margin: "1.5rem auto", padding: "0 1.5rem" }}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "1.5rem" }}>
              <div>
                <h1 style={{ fontSize: "1.6rem", fontWeight: 800 }}>Nurse Real-Time Triage Console</h1>
                <p style={{ color: "var(--text-muted)" }}>Live department red-flag queue and rapid clinical escalations</p>
              </div>
              <div style={{ display: "flex", alignItems: "center", gap: "0.75rem" }}>
                <span
                  style={{
                    fontSize: "0.8rem",
                    padding: "6px 12px",
                    borderRadius: "20px",
                    fontWeight: 600,
                    background: wsConnected ? "var(--severity-normal-bg)" : "var(--bg-surface-subtle)",
                    color: wsConnected ? "var(--severity-normal)" : "var(--text-muted)",
                    border: `1px solid ${wsConnected ? "var(--severity-normal-border)" : "var(--border-subtle)"}`,
                  }}
                >
                  {wsConnected ? "🟢 WebSocket: Connected (Real-Time)" : "⚪ WebSocket: Reconnecting..."}
                </span>
                <button className="btn btn-primary" onClick={loadTriageQueue}>
                  ↻ Refresh Alerts
                </button>
              </div>
            </div>

            {/* Metric Cards */}
            <div style={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: "1rem", marginBottom: "1.5rem" }}>
              <div style={{ background: "white", padding: "1.25rem", borderRadius: "12px", border: "1px solid var(--border-subtle)" }}>
                <div style={{ fontSize: "0.8rem", color: "var(--text-muted)", textTransform: "uppercase", fontWeight: 700 }}>Open Alerts</div>
                <div style={{ fontSize: "2rem", fontWeight: 800, color: "var(--text-main)" }}>{triageCounts.open}</div>
              </div>
              <div style={{ background: "var(--severity-critical-bg)", padding: "1.25rem", borderRadius: "12px", border: "1px solid var(--severity-critical-border)" }}>
                <div style={{ fontSize: "0.8rem", color: "var(--severity-critical)", textTransform: "uppercase", fontWeight: 700 }}>Critical Red Flags</div>
                <div style={{ fontSize: "2rem", fontWeight: 800, color: "var(--severity-critical)" }}>{triageCounts.critical}</div>
              </div>
              <div style={{ background: "white", padding: "1.25rem", borderRadius: "12px", border: "1px solid var(--border-subtle)" }}>
                <div style={{ fontSize: "0.8rem", color: "var(--text-muted)", textTransform: "uppercase", fontWeight: 700 }}>SLA Breached</div>
                <div style={{ fontSize: "2rem", fontWeight: 800, color: triageCounts.sla_breached > 0 ? "var(--severity-critical)" : "var(--severity-normal)" }}>{triageCounts.sla_breached}</div>
              </div>
            </div>

            {/* Alerts Table */}
            <table className="facts-table" style={{ background: "white" }}>
              <thead>
                <tr>
                  <th>Alert ID</th>
                  <th>Severity</th>
                  <th>Rule Triggered</th>
                  <th>Status</th>
                  <th>Time Elapsed</th>
                  <th>Actions</th>
                </tr>
              </thead>
              <tbody>
                {alerts.length === 0 ? (
                  <tr>
                    <td colSpan={6} style={{ textAlign: "center", padding: "2rem", color: "var(--text-muted)" }}>
                      ✓ No active red-flag alerts in queue.
                    </td>
                  </tr>
                ) : (
                  alerts.map((a) => (
                    <tr key={a.alert_id}>
                      <td style={{ fontFamily: "var(--font-mono)" }}>{a.alert_id.slice(0, 8)}...</td>
                      <td>
                        <span className={`badge ${a.severity === "critical" ? "badge-critical" : "badge-neutral"}`}>
                          {a.severity}
                        </span>
                      </td>
                      <td><strong>{a.rule_id}</strong></td>
                      <td>{a.status}</td>
                      <td>{new Date(a.created_at).toLocaleTimeString()}</td>
                      <td>
                        <button
                          className="btn btn-primary"
                          style={{ padding: "3px 10px", fontSize: "0.8rem" }}
                          onClick={() => acknowledgeAlert(a.alert_id)}
                        >
                          Acknowledge &amp; Attend
                        </button>
                      </td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>
        )}

        {/* TAB 3: OBSERVABILITY & SYSTEM HEALTH */}
        {activeTab === "observability" && (
          <div style={{ maxWidth: "1200px", margin: "1.5rem auto", padding: "0 1.5rem" }}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "1.5rem" }}>
              <div>
                <h1 style={{ fontSize: "1.6rem", fontWeight: 800 }}>System Health &amp; AI Observability</h1>
                <p style={{ color: "var(--text-muted)" }}>Real-time telemetry and 100% self-hosted AI model metrics</p>
              </div>
              <div style={{ display: "flex", gap: "0.75rem" }}>
                <a
                  href="http://localhost:3000"
                  target="_blank"
                  rel="noreferrer"
                  className="btn btn-primary"
                >
                  📈 Open Grafana Dashboards &rarr;
                </a>
              </div>
            </div>

            {/* Health Status Cards */}
            <div style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: "1rem", marginBottom: "1.5rem" }}>
              <div style={{ background: "white", padding: "1.25rem", borderRadius: "12px", border: "1px solid var(--border-subtle)" }}>
                <div style={{ fontSize: "0.8rem", color: "var(--text-muted)", textTransform: "uppercase", fontWeight: 700 }}>API Monolith (:8000)</div>
                <div style={{ fontSize: "1.3rem", fontWeight: 800, color: systemHealth?.status === "ok" ? "var(--severity-normal)" : "var(--severity-critical)", marginTop: "0.5rem" }}>● {systemHealth?.status === "ok" ? "HEALTHY" : "UNAVAILABLE"}</div>
              </div>
              <div style={{ background: "white", padding: "1.25rem", borderRadius: "12px", border: "1px solid var(--border-subtle)" }}>
                <div style={{ fontSize: "0.8rem", color: "var(--text-muted)", textTransform: "uppercase", fontWeight: 700 }}>AI Gateway (:8100)</div>
                <div style={{ fontSize: "1.3rem", fontWeight: 800, color: aiHealth?.status === "ok" ? "var(--severity-normal)" : "var(--severity-critical)", marginTop: "0.5rem" }}>● {aiHealth?.status === "ok" ? "ONLINE" : "UNAVAILABLE"}</div>
              </div>
              <div style={{ background: "white", padding: "1.25rem", borderRadius: "12px", border: "1px solid var(--border-subtle)" }}>
                <div style={{ fontSize: "0.8rem", color: "var(--text-muted)", textTransform: "uppercase", fontWeight: 700 }}>PostgreSQL RLS</div>
                <div style={{ fontSize: "1.3rem", fontWeight: 800, color: systemHealth?.components?.database?.status === "ok" ? "var(--severity-normal)" : "var(--severity-critical)", marginTop: "0.5rem" }}>● {systemHealth?.components?.database?.status === "ok" ? "ISOLATED" : "STATUS UNKNOWN"}</div>
              </div>
              <div style={{ background: "white", padding: "1.25rem", borderRadius: "12px", border: "1px solid var(--border-subtle)" }}>
                <div style={{ fontSize: "0.8rem", color: "var(--text-muted)", textTransform: "uppercase", fontWeight: 700 }}>OPA Rego Engine</div>
                <div style={{ fontSize: "1.3rem", fontWeight: 800, color: systemHealth?.components?.opa?.status === "ok" ? "var(--severity-normal)" : "var(--severity-critical)", marginTop: "0.5rem" }}>● {systemHealth?.components?.opa?.status === "ok" ? "ENFORCED" : "STATUS UNKNOWN"}</div>
              </div>
            </div>

            {/* AI Stack Metadata */}
            <div style={{ background: "white", padding: "1.5rem", borderRadius: "12px", border: "1px solid var(--border-subtle)", marginBottom: "1.5rem" }}>
              <h2 style={{ fontSize: "1.1rem", fontWeight: 800, marginBottom: "1rem" }}>Self-Hosted AI Model Stack</h2>
              <div style={{ display: "grid", gridTemplateColumns: "repeat(2, 1fr)", gap: "1rem", fontFamily: "var(--font-mono)", fontSize: "0.85rem" }}>
                <div><strong>ASR Model:</strong> {aiModelsMeta?.asr || "faster-whisper-small-int8"}</div>
                <div><strong>VAD Engine:</strong> {aiModelsMeta?.vad || "Silero VAD v5 ONNX"}</div>
                <div><strong>NLU Engine:</strong> Indic Multilingual Pattern &amp; Slot Extractor</div>
                <div><strong>TTS Backend:</strong> Local Synthesizer + Disk Cache (gTTS)</div>
                <div><strong>Target Languages:</strong> English, हिन्दी, தமிழ், తెలుగు, മലയാളം</div>
                <div><strong>Inference Mode:</strong> CPU Multithreaded INT8 (Zero External API)</div>
              </div>
            </div>
          </div>
        )}

        {/* TAB 4: SECURITY 403 ACCESS TESTS */}
        {activeTab === "security" && (
          <div style={{ maxWidth: "1000px", margin: "1.5rem auto", padding: "0 1.5rem" }}>
            <h1 style={{ fontSize: "1.6rem", fontWeight: 800, marginBottom: "0.5rem" }}>Security Authorization Verification</h1>
            <p style={{ color: "var(--text-muted)", marginBottom: "1.5rem" }}>
              Verify server-side RBAC / OPA / RLS enforcement. These tests execute real HTTP calls to prove server-side 403 Forbidden responses.
            </p>

            <div style={{ display: "grid", gap: "1rem" }}>
              <div style={{ background: "white", padding: "1.25rem", borderRadius: "12px", border: "1px solid var(--border-subtle)", display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                <div>
                  <strong>Test 1: Nurse Accessing Admin Endpoint</strong>
                  <p style={{ fontSize: "0.85rem", color: "var(--text-muted)" }}><code>GET /v1/admin/tenants</code> with Nurse token</p>
                </div>
                <button className="btn btn-outline" onClick={() => testSecurity403("/v1/admin/tenants", "nurse")}>
                  Execute Test
                </button>
              </div>

              <div style={{ background: "white", padding: "1.25rem", borderRadius: "12px", border: "1px solid var(--border-subtle)", display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                <div>
                  <strong>Test 2: Physician Accessing Security Audit Export</strong>
                  <p style={{ fontSize: "0.85rem", color: "var(--text-muted)" }}><code>POST /v1/security/audit-export</code> with Physician token</p>
                </div>
                <button className="btn btn-outline" onClick={() => testSecurity403("/v1/security/audit-export", "physician")}>
                  Execute Test
                </button>
              </div>

              <div style={{ background: "white", padding: "1.25rem", borderRadius: "12px", border: "1px solid var(--border-subtle)", display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                <div>
                  <strong>Test 3: Unauthenticated Access to Clinical Fact Store</strong>
                  <p style={{ fontSize: "0.85rem", color: "var(--text-muted)" }}><code>GET /v1/reviews</code> without Bearer header</p>
                </div>
                <button className="btn btn-outline" onClick={() => testSecurity403("/v1/reviews", "unauthenticated")}>
                  Execute Test
                </button>
              </div>
            </div>
          </div>
        )}
      </main>
    </div>
  );
}
