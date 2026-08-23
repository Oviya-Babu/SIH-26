import { useState, useEffect } from 'react';
import { api } from '../../services/api';

export default function StaffConsole() {
  const [alerts, setAlerts] = useState<any[]>([]);
  const [sessions, setSessions] = useState<any[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [view, setView] = useState<'alerts' | 'sessions'>('alerts');

  useEffect(() => {
    loadData();
    const interval = setInterval(loadData, 5000); // Poll every 5s
    return () => clearInterval(interval);
  }, []);

  const loadData = async () => {
    try {
      const [alertsRes, sessionsRes] = await Promise.all([
        api.getActiveAlerts(),
        api.getSessions(),
      ]);
      setAlerts(alertsRes.data || []);
      setSessions(sessionsRes.data || []);
    } catch (e) {
      console.error(e);
    }
    setIsLoading(false);
  };

  const handleAcknowledge = async (alertId: string) => {
    try {
      await api.acknowledgeAlert(alertId, 'user-nurse-1');
      loadData();
    } catch (e) {
      console.error(e);
    }
  };

  const handleResolve = async (alertId: string, falsePositive: boolean = false) => {
    try {
      await api.resolveAlert(alertId, falsePositive);
      loadData();
    } catch (e) {
      console.error(e);
    }
  };

  const getTimeSince = (timestamp: string) => {
    const minutes = Math.floor((Date.now() - new Date(timestamp).getTime()) / 60000);
    if (minutes < 1) return 'Just now';
    if (minutes < 60) return `${minutes}m ago`;
    return `${Math.floor(minutes / 60)}h ${minutes % 60}m ago`;
  };

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
          <div className="sidebar-section-label">Triage / Nurse</div>
          <div className={`sidebar-link ${view === 'alerts' ? 'sidebar-link--active' : ''}`} onClick={() => setView('alerts')} style={{ cursor: 'pointer' }}>
            <span className="sidebar-link-icon">🚨</span>
            Red Flag Alerts
            {alerts.length > 0 && <span className="sidebar-link-badge">{alerts.length}</span>}
          </div>
          <div className={`sidebar-link ${view === 'sessions' ? 'sidebar-link--active' : ''}`} onClick={() => setView('sessions')} style={{ cursor: 'pointer' }}>
            <span className="sidebar-link-icon">👥</span>
            Active Sessions
          </div>
        </div>
        <div className="sidebar-footer">
          <div className="flex items-center gap-3">
            <div style={{ width: 36, height: 36, borderRadius: '50%', background: 'var(--gradient-accent)', display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: 14 }}>🩺</div>
            <div>
              <div className="text-sm font-semibold">Priya Nair</div>
              <div className="text-xs text-muted">OPD Triage</div>
            </div>
          </div>
        </div>
      </nav>

      <main className="page-content">
        <div className="content-container">
          {view === 'alerts' ? (
            <>
              <div className="page-header">
                <div>
                  <h1 className="page-title">Red Flag Alerts</h1>
                  <p className="page-subtitle">Real-time patient safety alerts requiring immediate attention</p>
                </div>
                <span className={`badge ${alerts.length > 0 ? 'badge--danger' : 'badge--success'} badge--lg`}>
                  <span className="badge-dot badge-dot--pulse" />
                  {alerts.length > 0 ? `${alerts.length} Active Alert${alerts.length > 1 ? 's' : ''}` : 'No Active Alerts'}
                </span>
              </div>

              {alerts.length > 0 ? (
                <div style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
                  {alerts.sort((a, b) => {
                    const severityOrder: Record<string, number> = { critical: 0, high: 1, moderate: 2 };
                    return (severityOrder[a.severity] || 3) - (severityOrder[b.severity] || 3);
                  }).map(alert => (
                    <div key={alert.id} className={`redflag-card ${alert.severity === 'critical' ? 'redflag-card--critical' : ''}`}>
                      <div className="redflag-header">
                        <div className="redflag-severity">
                          <div className="redflag-severity-icon">
                            {alert.severity === 'critical' ? '🔴' : alert.severity === 'high' ? '🟠' : '🟡'}
                          </div>
                          <div>
                            <div className="redflag-title">{alert.ruleName}</div>
                            <div className="text-xs text-muted" style={{ marginTop: '2px' }}>
                              {alert.severity.toUpperCase()} • Fired {getTimeSince(alert.firedAt)}
                            </div>
                          </div>
                        </div>
                        <div className="redflag-timer">
                          SLA: {alert.slaMinutes || 5}min
                        </div>
                      </div>

                      <div className="redflag-patient-info">
                        <div><span className="glance-label">Patient</span> {alert.patient?.firstName} {alert.patient?.lastName}</div>
                        <div><span className="glance-label">MRN</span> {alert.patient?.hospitalLocalId}</div>
                        <div><span className="glance-label">Age/Sex</span> {alert.patient?.age}/{alert.patient?.sex}</div>
                        <div><span className="glance-label">Channel</span> {alert.session?.channel}</div>
                      </div>

                      <div className="redflag-details">{alert.staffMessage}</div>

                      {alert.matchedConditions && (
                        <div style={{ marginBottom: '16px' }}>
                          <div className="text-xs font-semibold text-muted" style={{ marginBottom: '6px' }}>MATCHED CONDITIONS</div>
                          <div className="flex gap-2" style={{ flexWrap: 'wrap' }}>
                            {Object.entries(alert.matchedConditions).map(([key, val]) => (
                              <span key={key} className="badge badge--danger">
                                {key}: {String(val)}
                              </span>
                            ))}
                          </div>
                        </div>
                      )}

                      <div className="flex gap-3">
                        {alert.status === 'active' && (
                          <button className="btn btn--primary btn--lg" onClick={() => handleAcknowledge(alert.id)}>
                            ✓ Acknowledge
                          </button>
                        )}
                        {(alert.status === 'active' || alert.status === 'acknowledged') && (
                          <>
                            <button className="btn btn--success" onClick={() => handleResolve(alert.id)}>
                              Resolve
                            </button>
                            <button className="btn btn--ghost" onClick={() => handleResolve(alert.id, true)}>
                              Mark False Positive
                            </button>
                          </>
                        )}
                      </div>
                    </div>
                  ))}
                </div>
              ) : (
                <div className="empty-state">
                  <div className="empty-state-icon">✅</div>
                  <div className="empty-state-title">No Active Alerts</div>
                  <div className="empty-state-description">All clear — no patients currently require urgent attention.</div>
                </div>
              )}
            </>
          ) : (
            <>
              <div className="page-header">
                <div>
                  <h1 className="page-title">Active Sessions</h1>
                  <p className="page-subtitle">Monitor all ongoing patient intake sessions</p>
                </div>
              </div>

              <div className="grid grid-auto">
                {sessions.map(s => {
                  const statusColors: Record<string, string> = {
                    intake_active: 'badge--primary',
                    intake_paused: 'badge--warning',
                    intake_complete: 'badge--success',
                    awaiting_review: 'badge--success',
                    abandoned: 'badge--default',
                  };
                  const statusLabels: Record<string, string> = {
                    intake_active: 'Active',
                    intake_paused: 'Paused',
                    intake_complete: 'Complete',
                    awaiting_review: 'Awaiting Review',
                    under_review: 'Under Review',
                    reviewed: 'Reviewed',
                    abandoned: 'Abandoned',
                  };

                  return (
                    <div key={s.id} className={`card ${s.activeAlerts?.length ? 'card--danger' : ''}`}>
                      <div className="card-header">
                        <div>
                          <div className="card-title">{s.patient?.firstName} {s.patient?.lastName}</div>
                          <div className="card-subtitle">{s.patient?.hospitalLocalId}</div>
                        </div>
                        <span className={`badge ${statusColors[s.status] || 'badge--default'}`}>
                          <span className="badge-dot badge-dot--pulse" />
                          {statusLabels[s.status] || s.status}
                        </span>
                      </div>
                      <div style={{ marginTop: '12px' }}>
                        <div className="progress" style={{ marginBottom: '6px' }}>
                          <div className="progress-bar" style={{ width: `${s.completenessScore * 100}%` }} />
                        </div>
                        <div className="flex justify-between text-xs text-muted">
                          <span>{Math.round(s.completenessScore * 100)}% complete</span>
                          <span>{s.channel} • {s.language.toUpperCase()}</span>
                        </div>
                      </div>
                      {s.activeAlerts?.length > 0 && (
                        <div style={{ marginTop: '12px' }}>
                          {s.activeAlerts.map((a: any) => (
                            <span key={a.id} className="badge badge--danger" style={{ marginRight: '4px' }}>
                              🚨 {a.ruleName}
                            </span>
                          ))}
                        </div>
                      )}
                      <div className="text-xs text-muted" style={{ marginTop: '12px' }}>
                        Started {getTimeSince(s.startedAt)}
                      </div>
                    </div>
                  );
                })}
              </div>

              {sessions.length === 0 && !isLoading && (
                <div className="empty-state">
                  <div className="empty-state-icon">👥</div>
                  <div className="empty-state-title">No Active Sessions</div>
                  <div className="empty-state-description">Patient intake sessions will appear here.</div>
                </div>
              )}
            </>
          )}
        </div>
      </main>
    </div>
  );
}
