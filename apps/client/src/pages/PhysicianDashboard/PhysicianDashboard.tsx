import { useState, useEffect } from 'react';
import { api } from '../../services/api';
import './PhysicianDashboard.css';

type View = 'queue' | 'summary';

export default function PhysicianDashboard() {
  const [view, setView] = useState<View>('queue');
  const [sessions, setSessions] = useState<any[]>([]);
  const [selectedSession, setSelectedSession] = useState<string | null>(null);
  const [summaryData, setSummaryData] = useState<any>(null);
  const [timeline, setTimeline] = useState<any[]>([]);
  const [facts, setFacts] = useState<any[]>([]);
  const [activeTab, setActiveTab] = useState<'summary' | 'timeline' | 'documents' | 'facts'>('summary');
  const [isLoading, setIsLoading] = useState(true);

  useEffect(() => {
    loadSessions();
  }, []);

  const loadSessions = async () => {
    try {
      const res = await api.getSessions();
      setSessions(res.data || []);
    } catch (e) {
      console.error(e);
    }
    setIsLoading(false);
  };

  const selectSession = async (sessionId: string) => {
    setSelectedSession(sessionId);
    setView('summary');
    setIsLoading(true);

    try {
      const [summaryRes, timelineRes, factsRes] = await Promise.all([
        api.getSummary(sessionId),
        api.getTimeline(sessionId),
        api.getSessionFacts(sessionId),
      ]);
      setSummaryData(summaryRes.data);
      setTimeline(timelineRes.data || []);
      setFacts(factsRes.data || []);
    } catch (e) {
      console.error(e);
    }
    setIsLoading(false);
  };

  const handleSectionReview = async (sectionId: string, action: string, editedContent?: string) => {
    if (!summaryData?.summary) return;
    try {
      const res = await api.reviewSummary(summaryData.summary.id, {
        sectionId,
        action,
        editedContent,
        physicianId: 'user-physician-1',
      });
      setSummaryData((prev: any) => ({ ...prev, summary: res.data }));
    } catch (e) {
      console.error(e);
    }
  };

  const handleBulkApprove = async () => {
    if (!summaryData?.summary) return;
    try {
      const res = await api.reviewSummary(summaryData.summary.id, {
        action: 'accept',
        physicianId: 'user-physician-1',
      });
      setSummaryData((prev: any) => ({ ...prev, summary: res.data }));
    } catch (e) {
      console.error(e);
    }
  };

  const getSeverityColor = (severity: string) => {
    switch (severity) {
      case 'critical': return 'var(--color-danger)';
      case 'high': return 'var(--color-warning)';
      default: return 'var(--color-info)';
    }
  };

  // ─── Queue View ────────────────────────────────────────────────────────

  if (view === 'queue') {
    const readySessions = sessions.filter(s =>
      ['awaiting_review', 'under_review', 'intake_complete'].includes(s.status)
    );
    const activeSessions = sessions.filter(s =>
      ['intake_active', 'intake_paused'].includes(s.status)
    );

    return (
      <div className="page-layout">
        <nav className="sidebar">
          <div className="sidebar-header">
            <div className="sidebar-logo">
              <div className="sidebar-logo-icon">M</div>
              <div className="sidebar-logo-text"><span>MediKiosk</span></div>
            </div>
          </div>
          <div className="sidebar-nav">
            <div className="sidebar-section-label">Physician</div>
            <div className="sidebar-link sidebar-link--active">
              <span className="sidebar-link-icon">📋</span>
              Patient Queue
              {readySessions.length > 0 && <span className="sidebar-link-badge">{readySessions.length}</span>}
            </div>
          </div>
          <div className="sidebar-footer">
            <div className="flex items-center gap-3">
              <div style={{ width: 36, height: 36, borderRadius: '50%', background: 'var(--gradient-success)', display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: 14 }}>👨‍⚕️</div>
              <div>
                <div className="text-sm font-semibold">Dr. Arun Sharma</div>
                <div className="text-xs text-muted">Internal Medicine</div>
              </div>
            </div>
          </div>
        </nav>

        <main className="page-content">
          <div className="content-container">
            <div className="page-header">
              <div>
                <h1 className="page-title">Patient Queue</h1>
                <p className="page-subtitle">Review AI-drafted clinical summaries before consultation</p>
              </div>
              <div className="flex gap-3">
                <span className="badge badge--success badge--lg">
                  <span className="badge-dot" /> {readySessions.length} Ready for Review
                </span>
                <span className="badge badge--primary badge--lg">
                  <span className="badge-dot badge-dot--pulse" /> {activeSessions.length} In Progress
                </span>
              </div>
            </div>

            {/* Ready for Review */}
            {readySessions.length > 0 && (
              <div style={{ marginBottom: '32px' }}>
                <h2 className="text-lg font-semibold" style={{ marginBottom: '16px', display: 'flex', alignItems: 'center', gap: '8px' }}>
                  <span style={{ color: 'var(--color-success)' }}>●</span> Ready for Review
                </h2>
                <div className="grid grid-auto">
                  {readySessions.map(s => (
                    <div key={s.id} className={`card card--interactive ${s.activeAlerts?.length ? 'card--danger' : ''}`} onClick={() => selectSession(s.id)}>
                      <div className="card-header">
                        <div>
                          <div className="card-title">{s.patient?.firstName} {s.patient?.lastName}</div>
                          <div className="card-subtitle">MRN: {s.patient?.hospitalLocalId}</div>
                        </div>
                        {s.activeAlerts?.length > 0 && (
                          <span className="badge badge--danger">
                            <span className="badge-dot badge-dot--pulse" />
                            {s.activeAlerts.length} Red Flag{s.activeAlerts.length > 1 ? 's' : ''}
                          </span>
                        )}
                      </div>
                      <div className="card-body">
                        <div className="flex gap-3" style={{ marginBottom: '12px', flexWrap: 'wrap' }}>
                          <span className="glance-item">
                            <span className="glance-label">Age</span> {s.patient?.age || '—'}
                          </span>
                          <span className="glance-item">
                            <span className="glance-label">Sex</span> {s.patient?.sex || '—'}
                          </span>
                          <span className="glance-item">
                            <span className="glance-label">Channel</span> {s.channel}
                          </span>
                        </div>
                        <div className="progress" style={{ marginBottom: '8px' }}>
                          <div className="progress-bar" style={{ width: `${s.completenessScore * 100}%` }} />
                        </div>
                        <div className="text-xs text-muted">{Math.round(s.completenessScore * 100)}% complete</div>
                      </div>
                      <div className="card-footer">
                        <span className="text-xs text-muted">
                          Started {new Date(s.startedAt).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
                        </span>
                        <button className="btn btn--primary btn--sm" style={{ marginLeft: 'auto' }}>
                          Review →
                        </button>
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            )}

            {/* Active Sessions */}
            {activeSessions.length > 0 && (
              <div>
                <h2 className="text-lg font-semibold" style={{ marginBottom: '16px', display: 'flex', alignItems: 'center', gap: '8px' }}>
                  <span style={{ color: 'var(--color-accent-primary)' }}>●</span> Currently in Intake
                </h2>
                <div className="grid grid-auto">
                  {activeSessions.map(s => (
                    <div key={s.id} className="card" style={{ opacity: 0.75 }}>
                      <div className="card-header">
                        <div>
                          <div className="card-title">{s.patient?.firstName} {s.patient?.lastName}</div>
                          <div className="card-subtitle">MRN: {s.patient?.hospitalLocalId}</div>
                        </div>
                        <span className="badge badge--primary">
                          <span className="badge-dot badge-dot--pulse" />
                          {s.status === 'intake_paused' ? 'Paused' : 'Active'}
                        </span>
                      </div>
                      <div className="progress" style={{ marginTop: '12px' }}>
                        <div className="progress-bar" style={{ width: `${s.completenessScore * 100}%` }} />
                      </div>
                      <div className="text-xs text-muted" style={{ marginTop: '4px' }}>
                        {Math.round(s.completenessScore * 100)}% complete — {s.channel}
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            )}

            {sessions.length === 0 && !isLoading && (
              <div className="empty-state">
                <div className="empty-state-icon">📋</div>
                <div className="empty-state-title">No patients in queue</div>
                <div className="empty-state-description">Patients will appear here as they complete their pre-consultation intake.</div>
              </div>
            )}
          </div>
        </main>
      </div>
    );
  }

  // ─── Summary Review View ───────────────────────────────────────────────

  const summary = summaryData?.summary;
  const patient = summaryData?.patient;

  return (
    <div className="page-layout">
      <nav className="sidebar">
        <div className="sidebar-header">
          <div className="sidebar-logo">
            <div className="sidebar-logo-icon">M</div>
            <div className="sidebar-logo-text"><span>MediKiosk</span></div>
          </div>
        </div>
        <div className="sidebar-nav">
          <div className="sidebar-section-label">Physician</div>
          <div className="sidebar-link" onClick={() => { setView('queue'); setSelectedSession(null); }} style={{ cursor: 'pointer' }}>
            <span className="sidebar-link-icon">←</span>
            Back to Queue
          </div>
          <div className="sidebar-link sidebar-link--active">
            <span className="sidebar-link-icon">📋</span>
            Summary Review
          </div>
        </div>
        <div className="sidebar-footer">
          <div className="flex items-center gap-3">
            <div style={{ width: 36, height: 36, borderRadius: '50%', background: 'var(--gradient-success)', display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: 14 }}>👨‍⚕️</div>
            <div>
              <div className="text-sm font-semibold">Dr. Arun Sharma</div>
              <div className="text-xs text-muted">Internal Medicine</div>
            </div>
          </div>
        </div>
      </nav>

      <main className="page-content">
        <div className="content-container">
          {isLoading ? (
            <div className="empty-state">
              <div className="spinner spinner--lg" style={{ margin: '0 auto' }} />
              <p className="text-secondary" style={{ marginTop: '16px' }}>Loading clinical summary...</p>
            </div>
          ) : summary ? (
            <>
              {/* Patient Header */}
              <div className="page-header">
                <div>
                  <h1 className="page-title">{patient?.firstName} {patient?.lastName}</h1>
                  <p className="page-subtitle">
                    MRN: {patient?.hospitalLocalId} • {patient?.sex}, {patient?.age}y •
                    Language: {patient?.language?.toUpperCase()}
                  </p>
                </div>
                <div className="flex gap-3 items-center">
                  <div className="draft-badge">
                    <span className="draft-badge-dot" />
                    {summary.status === 'approved' ? 'APPROVED' : 'AI DRAFT — UNVERIFIED'}
                  </div>
                  {summary.status !== 'approved' && (
                    <button className="btn btn--success btn--lg" onClick={handleBulkApprove}>
                      ✓ Approve All & Sign
                    </button>
                  )}
                </div>
              </div>

              {/* At-a-glance Strip */}
              <div className="summary-glance-strip">
                {summary.redFlags?.length > 0 && summary.redFlags.map((rf: any) => (
                  <div key={rf.id} className="glance-item glance-item--danger">
                    <span>🚨</span>
                    <span className="font-semibold">{rf.ruleName}</span>
                  </div>
                ))}
                {facts.filter((f: any) => f.category === 'allergy').map((f: any) => (
                  <div key={f.id} className="glance-item glance-item--danger">
                    <span>⚠️ Allergy:</span>
                    <span className="font-semibold">{f.valueNormalized}</span>
                  </div>
                ))}
                {facts.filter((f: any) => f.category === 'medication').map((f: any) => (
                  <div key={f.id} className="glance-item">
                    <span className="glance-label">Med</span>
                    <span>{f.valueNormalized}</span>
                  </div>
                ))}
                <div className="glance-item">
                  <span className="glance-label">Completeness</span>
                  <span className="font-semibold">{Math.round(summary.completenessScore * 100)}%</span>
                </div>
              </div>

              {/* Tabs */}
              <div className="tabs">
                {(['summary', 'timeline', 'facts'] as const).map(tab => (
                  <button
                    key={tab}
                    className={`tab ${activeTab === tab ? 'tab--active' : ''}`}
                    onClick={() => setActiveTab(tab)}
                  >
                    {tab === 'summary' ? '📝 Summary' : tab === 'timeline' ? '📅 Timeline' : '🔬 Clinical Facts'}
                  </button>
                ))}
              </div>

              {/* Summary Tab */}
              {activeTab === 'summary' && (
                <>
                  {/* Conflicts */}
                  {summary.conflicts?.length > 0 && (
                    <div style={{ marginBottom: '24px' }}>
                      <h3 className="text-base font-semibold text-warning" style={{ marginBottom: '12px' }}>⚠️ Conflicting Information</h3>
                      {summary.conflicts.map((c: any) => (
                        <div key={c.id} className="conflict-card" style={{ marginBottom: '12px' }}>
                          <div className="conflict-header">
                            <span>⚠</span> {c.fieldLabel}
                          </div>
                          {c.sources.map((src: any, i: number) => (
                            <div key={i} className="conflict-source">
                              <span className="conflict-source-bullet" />
                              <div>
                                <div className="conflict-source-value">{src.value}</div>
                                <div className="conflict-source-meta">{src.source}</div>
                              </div>
                            </div>
                          ))}
                        </div>
                      ))}
                    </div>
                  )}

                  {/* Missing Info */}
                  {summary.missingInformation?.filter((m: any) => m.priority === 'required').length > 0 && (
                    <div className="alert alert--warning" style={{ marginBottom: '24px' }}>
                      <span className="alert-icon">📋</span>
                      <div className="alert-content">
                        <div className="alert-title">Missing Required Information</div>
                        <ul style={{ marginTop: '4px', paddingLeft: '16px' }}>
                          {summary.missingInformation.filter((m: any) => m.priority === 'required').map((m: any) => (
                            <li key={m.fieldName} style={{ fontSize: '13px' }}>{m.fieldLabel}</li>
                          ))}
                        </ul>
                      </div>
                    </div>
                  )}

                  {/* Summary Sections */}
                  {summary.sections?.map((section: any) => (
                    <div key={section.id} className="summary-section">
                      <div className="summary-section-header">
                        <div className="summary-section-title">
                          {section.title}
                          {section.reviewStatus === 'accepted' && <span className="badge badge--success badge--sm" style={{ marginLeft: '8px' }}>✓ Accepted</span>}
                          {section.reviewStatus === 'edited' && <span className="badge badge--warning badge--sm" style={{ marginLeft: '8px' }}>✏ Edited</span>}
                          {section.reviewStatus === 'rejected' && <span className="badge badge--danger badge--sm" style={{ marginLeft: '8px' }}>✗ Rejected</span>}
                        </div>
                        {summary.status !== 'approved' && (
                          <div className="summary-section-actions">
                            <button className="btn btn--success btn--sm" onClick={() => handleSectionReview(section.id, 'accept')} title="Accept">✓</button>
                            <button className="btn btn--ghost btn--sm" onClick={() => {
                              const edited = prompt('Edit content:', section.editedContent || section.content);
                              if (edited) handleSectionReview(section.id, 'edit', edited);
                            }} title="Edit">✏️</button>
                            <button className="btn btn--ghost btn--sm" onClick={() => handleSectionReview(section.id, 'reject')} title="Reject">✗</button>
                          </div>
                        )}
                      </div>
                      <div className="summary-section-body">
                        {(section.editedContent || section.content).split('\n').map((line: string, i: number) => (
                          <div key={i}>{line}</div>
                        ))}
                        {section.facts?.length > 0 && (
                          <div className="text-xs text-muted" style={{ marginTop: '8px', fontStyle: 'italic' }}>
                            Based on {section.facts.length} clinical fact{section.facts.length > 1 ? 's' : ''}
                          </div>
                        )}
                      </div>
                    </div>
                  ))}
                </>
              )}

              {/* Timeline Tab */}
              {activeTab === 'timeline' && (
                <div className="timeline">
                  {timeline.map((event: any) => (
                    <div key={event.id} className="timeline-item">
                      <div className={`timeline-dot ${event.source === 'document_extracted' ? 'timeline-dot--document' : ''}`} />
                      <div className="timeline-date">{event.dateLabel}</div>
                      <div className="timeline-content">
                        <div className="timeline-title">{event.title}</div>
                        <div className="text-sm text-secondary" style={{ marginTop: '4px' }}>{event.description}</div>
                        <div className="timeline-source">
                          <span>{event.source === 'patient_reported' ? '🗣 Patient reported' : '📄 Document extracted'}</span>
                          {event.dateApproximate && <span className="badge badge--default" style={{ marginLeft: '8px' }}>≈ Approximate</span>}
                        </div>
                      </div>
                    </div>
                  ))}
                  {timeline.length === 0 && (
                    <div className="empty-state">
                      <div className="empty-state-icon">📅</div>
                      <div className="empty-state-title">No timeline events</div>
                    </div>
                  )}
                </div>
              )}

              {/* Facts Tab */}
              {activeTab === 'facts' && (
                <div className="table-container">
                  <table className="table">
                    <thead>
                      <tr>
                        <th>Category</th>
                        <th>Concept</th>
                        <th>Value</th>
                        <th>Source</th>
                        <th>Confidence</th>
                        <th>Conflict</th>
                      </tr>
                    </thead>
                    <tbody>
                      {facts.map((fact: any) => (
                        <tr key={fact.id}>
                          <td><span className="badge badge--default">{fact.category.replace(/_/g, ' ')}</span></td>
                          <td className="font-medium">{fact.conceptLabel}</td>
                          <td>{fact.valueNormalized || fact.valueRaw}</td>
                          <td>
                            <span className={`badge ${fact.source === 'patient_reported' ? 'badge--primary' : 'badge--default'}`}>
                              {fact.source === 'patient_reported' ? '🗣 Patient' : '📄 Document'}
                            </span>
                          </td>
                          <td>
                            <span className={`confidence ${fact.confidence >= 0.85 ? 'confidence--high' : fact.confidence >= 0.65 ? 'confidence--medium' : 'confidence--low'}`}>
                              {Math.round(fact.confidence * 100)}%
                            </span>
                          </td>
                          <td>
                            {fact.isConflicting && <span className="badge badge--warning">⚠ Conflict</span>}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </>
          ) : (
            <div className="empty-state">
              <div className="empty-state-icon">📋</div>
              <div className="empty-state-title">No summary available</div>
              <div className="empty-state-description">The patient's intake may not be complete yet.</div>
            </div>
          )}
        </div>
      </main>
    </div>
  );
}
