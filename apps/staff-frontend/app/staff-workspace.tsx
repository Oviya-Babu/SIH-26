"use client";

import { useCallback, useEffect, useState } from "react";

type ReviewQueueItem = {
  session_id: string;
  status: string;
  session_status: string;
  completeness: number;
  department_name: string;
  patient: { display: string; year_of_birth: number | null; gender: string | null };
  signals: { critical_alerts: number; high_alerts: number; unresolved_conflicts: number; fact_count: number };
};

type ReviewDetail = {
  session: { status: string; review_status: string; completeness: number; protocol: { family: string; version: string } };
  patient: { full_name: string; hospital_local_id: string | null; year_of_birth: number | null; gender: string | null } | null;
  facts: Array<{ fact_id: string; label: string; value: unknown; source_type: string; respondent_relationship: string | null; verification_status: string }>;
  red_flags: Array<{ severity: string; status: string; rule_id: string }>;
  gaps: { not_asked_due_to_escalation: Array<{ label: string }>; patient_did_not_know: Array<{ label: string }> };
};

type AuditEvent = { action: string; actor_role: string; occurred_at: string };

const apiOrigin = process.env.NEXT_PUBLIC_API_ORIGIN ?? "http://localhost:8000";
const issuer = (process.env.NEXT_PUBLIC_OIDC_ISSUER ?? "http://localhost:8080/realms/medikiosk").replace(/\/$/, "");
const clientId = process.env.NEXT_PUBLIC_OIDC_CLIENT_ID ?? "medikiosk-staff";
const stateKey = "medikiosk.oidc.state";
const verifierKey = "medikiosk.oidc.verifier";

function safeJson(value: unknown): string {
  try {
    return JSON.stringify(value);
  } catch {
    return "[unavailable]";
  }
}

function randomValue(): string {
  const bytes = crypto.getRandomValues(new Uint8Array(32));
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
}

function base64Url(bytes: ArrayBuffer): string {
  const source = new Uint8Array(bytes);
  let binary = "";
  source.forEach((byte) => {
    binary += String.fromCharCode(byte);
  });
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

async function challengeFor(verifier: string): Promise<string> {
  return base64Url(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier)));
}

export default function StaffWorkspace() {
  const [token, setToken] = useState<string | null>(null);
  const [queue, setQueue] = useState<ReviewQueueItem[]>([]);
  const [selected, setSelected] = useState<ReviewDetail | null>(null);
  const [history, setHistory] = useState<AuditEvent[]>([]);
  const [selectedSession, setSelectedSession] = useState<string | null>(null);
  const [status, setStatus] = useState("Signing in securely…");
  const [busy, setBusy] = useState(false);

  const request = useCallback(async <T,>(accessToken: string, path: string, init?: RequestInit): Promise<T> => {
    const headers = new Headers(init?.headers);
    headers.set("authorization", `Bearer ${accessToken}`);
    const response = await fetch(`${apiOrigin}${path}`, {
      ...init,
      headers,
      cache: "no-store",
    });
    if (!response.ok) {
      if (response.status === 401) setToken(null);
      throw new Error(`request_failed_${response.status}`);
    }
    return response.json() as Promise<T>;
  }, []);

  const loadQueue = useCallback(async (accessToken: string) => {
    const data = await request<{ reviews: ReviewQueueItem[] }>(accessToken, "/v1/reviews");
    setQueue(data.reviews);
    setStatus(data.reviews.length ? "Select a review to inspect the evidence." : "No reviews currently require action.");
  }, [request]);

  const loadDetail = useCallback(async (accessToken: string, sessionId: string) => {
    const [data, audit] = await Promise.all([
      request<ReviewDetail>(accessToken, `/v1/reviews/${encodeURIComponent(sessionId)}`),
      request<{ events: AuditEvent[] }>(accessToken, `/v1/reviews/${encodeURIComponent(sessionId)}/history`),
    ]);
    setSelected(data);
    setHistory(audit.events);
    setSelectedSession(sessionId);
  }, [request]);

  useEffect(() => {
    const callback = async () => {
      const params = new URLSearchParams(window.location.search);
      const code = params.get("code");
      const returnedState = params.get("state");
      if (!code) {
        const state = randomValue();
        const verifier = randomValue();
        sessionStorage.setItem(stateKey, state);
        sessionStorage.setItem(verifierKey, verifier);
        const authorization = new URL(`${issuer}/protocol/openid-connect/auth`);
        authorization.search = new URLSearchParams({
          client_id: clientId,
          redirect_uri: window.location.origin,
          response_type: "code",
          scope: "openid",
          state,
          code_challenge: await challengeFor(verifier),
          code_challenge_method: "S256",
        }).toString();
        window.location.assign(authorization.toString());
        return;
      }

      const expectedState = sessionStorage.getItem(stateKey);
      const verifier = sessionStorage.getItem(verifierKey);
      sessionStorage.removeItem(stateKey);
      sessionStorage.removeItem(verifierKey);
      window.history.replaceState({}, document.title, window.location.pathname);
      if (!expectedState || !verifier || returnedState !== expectedState) {
        setStatus("Sign-in could not be verified. Please try again.");
        return;
      }

      const response = await fetch(`${issuer}/protocol/openid-connect/token`, {
        method: "POST",
        headers: { "content-type": "application/x-www-form-urlencoded" },
        body: new URLSearchParams({
          grant_type: "authorization_code",
          client_id: clientId,
          code,
          redirect_uri: window.location.origin,
          code_verifier: verifier,
        }),
        cache: "no-store",
      });
      if (!response.ok) {
        setStatus("Sign-in did not complete. Please start again.");
        return;
      }
      const result = (await response.json()) as { access_token?: string };
      if (!result.access_token) {
        setStatus("Sign-in did not return an access token.");
        return;
      }
      setToken(result.access_token);
      await loadQueue(result.access_token);
    };
    void callback().catch(() => setStatus("Sign-in is unavailable. Please try again."));
  }, [loadQueue]);

  const openReview = async (sessionId: string) => {
    if (!token) return;
    setBusy(true);
    try {
      await request(token, `/v1/reviews/${encodeURIComponent(sessionId)}/open`, { method: "POST" });
      await loadDetail(token, sessionId);
      await loadQueue(token);
      setStatus("Review opened. Inspect facts and provenance before approving.");
    } catch {
      setStatus("The review could not be opened. Access is enforced by the API.");
    } finally {
      setBusy(false);
    }
  };

  const approve = async () => {
    if (!token || !selectedSession || !window.confirm("I attest that I reviewed this record. Approve for export?")) return;
    setBusy(true);
    try {
      await request(token, `/v1/summaries/${encodeURIComponent(selectedSession)}/approve`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ attestation: true, export_targets: ["fhir"] }),
      });
      await loadQueue(token);
      setStatus("Approved. The API recorded the attestation and queued the configured export.");
    } catch {
      setStatus("Approval was not completed. Resolve all API-reported review requirements first.");
    } finally {
      setBusy(false);
    }
  };

  if (!token) return <main className="centered"><p>{status}</p></main>;

  return (
    <main>
      <header>
        <div><p className="eyebrow">MediKiosk</p><h1>Physician review</h1></div>
        <button onClick={() => { setToken(null); setQueue([]); setSelected(null); setHistory([]); setStatus("Local session cleared. Reload to sign in again."); }}>Clear local session</button>
      </header>
      <p className="status" role="status">{status}</p>
      <section className="workspace">
        <aside aria-label="Review queue">
          <h2>Queue</h2>
          {queue.map((review) => <article key={review.session_id} className="queue-item">
            <strong>{review.patient.display}</strong>
            <span>{review.department_name} · {Math.round(review.completeness * 100)}% complete</span>
            <span>{review.signals.critical_alerts ? "Critical alert" : `${review.signals.fact_count} facts`}</span>
            <button disabled={busy} onClick={() => void openReview(review.session_id)}>Open review</button>
          </article>)}
        </aside>
        <section className="detail" aria-live="polite">
          {!selected && <p>Select a review from the queue.</p>}
          {selected && <>
            <div className="detail-header"><div><h2>{selected.patient?.full_name ?? "Patient"}</h2><p>{selected.session.protocol.family} {selected.session.protocol.version} · {Math.round(selected.session.completeness * 100)}% complete</p></div><button className="approve" disabled={busy || selected.session.review_status === "approved" || selected.session.review_status === "exported"} onClick={() => void approve()}>Attest &amp; approve</button></div>
            <h3>Red flags</h3><ul>{selected.red_flags.length ? selected.red_flags.map((flag) => <li key={`${flag.rule_id}-${flag.status}`}>{flag.severity}: {flag.rule_id} ({flag.status})</li>) : <li>None recorded</li>}</ul>
            <h3>Clinical facts and provenance</h3><div className="facts">{selected.facts.map((fact) => <article key={fact.fact_id}><strong>{fact.label}</strong><code>{safeJson(fact.value)}</code><span>{fact.source_type}{fact.respondent_relationship ? ` · reported by ${fact.respondent_relationship}` : ""} · {fact.verification_status}</span></article>)}</div>
            <h3>Interview gaps</h3><p>{selected.gaps.not_asked_due_to_escalation.length} not asked due to emergency escalation; {selected.gaps.patient_did_not_know.length} declined or unsure.</p>
            <h3>Review audit</h3><ul>{history.length ? history.map((event, index) => <li key={`${event.occurred_at}-${event.action}-${index}`}>{event.occurred_at}: {event.actor_role} — {event.action}</li>) : <li>No review events yet.</li>}</ul>
          </>}
        </section>
      </section>
    </main>
  );
}
