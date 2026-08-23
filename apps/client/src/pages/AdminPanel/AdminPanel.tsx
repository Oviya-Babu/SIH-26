import { useState, useEffect } from 'react';
import { api } from '../../services/api';

type AdminView = 'analytics' | 'protocols' | 'rules' | 'audit';

export default function AdminPanel() {
  const [view, setView] = useState<AdminView>('analytics');
  const [stats, setStats] = useState<any>(null);
  const [protocols, setProtocols] = useState<any[]>([]);
  const [rules, setRules] = useState<any[]>([]);
  const [auditEvents, setAuditEvents] = useState<any[]>([]);
  const [isLoading, setIsLoading] = useState(true);

  useEffect(() => {
    loadData();
  }, [view]);

  const loadData = async () => {
    setIsLoading(true);
    try {
      switch (view) {
        case 'analytics': {
          const res = await api.getStats();
          setStats(res.data);
          break;
        }
        case 'protocols': {
          const res = await api.getProtocols();
          setProtocols(res.data || []);
          break;
        }
        case 'rules': {
          const res = await api.getRules();
          setRules(res.data || []);
          break;
        }
        case 'audit': {
          const res = await api.getAuditEvents({ limit: '50' });
          setAuditEvents(res.data || []);
          break;
        }
      }
    } catch (e) {
      console.error(e);
    }
    setIsLoading(false);
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
          <div className="sidebar-section-label">Administration</div>
          {([
            { id: 'analytics', icon: '📊', label: 'Analytics' },
            { id: 'protocols', icon: '📋', label: 'Clinical Protocols' },
            { id: 'rules', icon: '🚨', label: 'Red-Flag Rules' },
            { id: 'audit', icon: '📜', label: 'Audit Trail' },
          ] as const).map(item => (
            <div
              key={item.id}
              className={`sidebar-link ${view === item.id ? 'sidebar-link--active' : ''}`}
              onClick={() => setView(item.id)}
              style={{ cursor: 'pointer' }}
            >
              <span className="sidebar-link-icon">{item.icon}</span>
              {item.label}
            </div>
          ))}
        </div>
        <div className="sidebar-footer">
          <div className="flex items-center gap-3">
            <div style={{ width: 36, height: 36, borderRadius: '50%', background: 'linear-gradient(135deg, #8b5cf6, #6d28d9)', display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: 14 }}>📊</div>
            <div>
              <div className="text-sm font-semibold">Rajesh Kumar</div>
              <div className="text-xs text-muted">Hospital Admin</div>
            </div>
          </div>
        </div>
      </nav>

      <main className="page-content">
        <div className="content-container">
          {/* Analytics View */}
          {view === 'analytics' && (
            <>
              <div className="page-header">
                <div>
                  <h1 className="page-title">Hospital Analytics</h1>
                  <p className="page-subtitle">Operational metrics and intake performance overview</p>
                </div>
              </div>

              {stats ? (
                <>
                  <div className="grid grid-4" style={{ marginBottom: '32px' }}>
                    {[
                      { label: 'Total Patients', value: stats.totalPatients, color: 'var(--color-accent-primary)' },
                      { label: 'Active Sessions', value: stats.activeSessions, color: 'var(--color-accent-secondary)' },
                      { label: 'Awaiting Review', value: stats.awaitingReview, color: 'var(--color-warning)' },
                      { label: 'Active Alerts', value: stats.activeAlerts, color: stats.activeAlerts > 0 ? 'var(--color-danger)' : 'var(--color-success)' },
                    ].map(stat => (
                      <div key={stat.label} className="card stat-card">
                        <div className="stat-card-label">{stat.label}</div>
                        <div className="stat-card-value" style={{ color: stat.color }}>{stat.value}</div>
                      </div>
                    ))}
                  </div>

                  <div className="grid grid-2" style={{ marginBottom: '32px' }}>
                    <div className="card">
                      <div className="card-header">
                        <div className="card-title">Sessions by Channel</div>
                      </div>
                      <div style={{ display: 'flex', flexDirection: 'column', gap: '12px', marginTop: '8px' }}>
                        {Object.entries(stats.sessionsByChannel || {}).map(([channel, count]) => (
                          <div key={channel} className="flex items-center gap-3">
                            <span className="text-sm" style={{ width: '60px', textTransform: 'capitalize' }}>{channel}</span>
                            <div className="progress" style={{ flex: 1 }}>
                              <div
                                className="progress-bar"
                                style={{
                                  width: `${stats.totalSessions > 0 ? ((count as number) / stats.totalSessions) * 100 : 0}%`,
                                }}
                              />
                            </div>
                            <span className="text-sm font-semibold" style={{ width: '30px', textAlign: 'right' }}>{count as number}</span>
                          </div>
                        ))}
                      </div>
                    </div>

                    <div className="card">
                      <div className="card-header">
                        <div className="card-title">Alerts by Severity</div>
                      </div>
                      <div style={{ display: 'flex', flexDirection: 'column', gap: '12px', marginTop: '8px' }}>
                        {[
                          { label: 'Critical', count: stats.alertsBySeverity?.critical || 0, color: 'var(--color-danger)' },
                          { label: 'High', count: stats.alertsBySeverity?.high || 0, color: 'var(--color-warning)' },
                          { label: 'Moderate', count: stats.alertsBySeverity?.moderate || 0, color: 'var(--color-info)' },
                        ].map(item => (
                          <div key={item.label} className="flex items-center gap-3">
                            <span className="text-sm" style={{ width: '80px' }}>{item.label}</span>
                            <div style={{ width: 40, height: 40, borderRadius: '50%', background: item.color, display: 'flex', alignItems: 'center', justifyContent: 'center', fontWeight: 700, fontSize: 16 }}>
                              {item.count}
                            </div>
                          </div>
                        ))}
                      </div>
                    </div>
                  </div>

                  <div className="card">
                    <div className="card-header">
                      <div className="card-title">System Overview</div>
                    </div>
                    <div className="grid grid-3" style={{ marginTop: '16px' }}>
                      <div>
                        <div className="text-xs text-muted font-semibold">TOTAL SESSIONS</div>
                        <div className="text-2xl font-bold">{stats.totalSessions}</div>
                      </div>
                      <div>
                        <div className="text-xs text-muted font-semibold">COMPLETED</div>
                        <div className="text-2xl font-bold text-success">{stats.completedSessions}</div>
                      </div>
                      <div>
                        <div className="text-xs text-muted font-semibold">AVG COMPLETENESS</div>
                        <div className="text-2xl font-bold">{Math.round((stats.avgCompleteness || 0) * 100)}%</div>
                      </div>
                    </div>
                  </div>
                </>
              ) : isLoading ? (
                <div className="empty-state"><div className="spinner spinner--lg" style={{ margin: '0 auto' }} /></div>
              ) : null}
            </>
          )}

          {/* Protocols View */}
          {view === 'protocols' && (
            <>
              <div className="page-header">
                <div>
                  <h1 className="page-title">Clinical Protocols</h1>
                  <p className="page-subtitle">Manage versioned questionnaire protocols — changes require clinical governance approval</p>
                </div>
              </div>

              {protocols.map(protocol => (
                <div key={protocol.id} className="card" style={{ marginBottom: '16px' }}>
                  <div className="card-header">
                    <div>
                      <div className="card-title">{protocol.name}</div>
                      <div className="card-subtitle">v{protocol.version} • {protocol.department} • {protocol.specialty}</div>
                    </div>
                    <span className="badge badge--success">
                      <span className="badge-dot" /> Active
                    </span>
                  </div>
                  <div className="card-body">
                    <div className="flex gap-4" style={{ marginTop: '8px' }}>
                      <span className="glance-item">
                        <span className="glance-label">Questions</span> {protocol.questionCount}
                      </span>
                      <span className="glance-item">
                        <span className="glance-label">ID</span> {protocol.id}
                      </span>
                    </div>
                    {protocol.groups && (
                      <div style={{ marginTop: '16px' }}>
                        <div className="text-xs font-semibold text-muted" style={{ marginBottom: '8px' }}>QUESTION GROUPS</div>
                        <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
                          {protocol.groups.map((g: any) => (
                            <div key={g.id} className="flex items-center gap-3">
                              <span className="text-sm" style={{ minWidth: '180px' }}>{g.label}</span>
                              <div className="progress" style={{ flex: 1, height: '6px' }}>
                                <div className="progress-bar" style={{ width: `${g.progress * 100}%` }} />
                              </div>
                              <span className="text-xs text-muted">{g.totalQuestions} Q</span>
                            </div>
                          ))}
                        </div>
                      </div>
                    )}
                  </div>
                  <div className="card-footer">
                    <span className="text-xs text-muted">Protocol changes require clinical governance review before activation</span>
                  </div>
                </div>
              ))}
            </>
          )}

          {/* Rules View */}
          {view === 'rules' && (
            <>
              <div className="page-header">
                <div>
                  <h1 className="page-title">Red-Flag Rules</h1>
                  <p className="page-subtitle">Deterministic safety rules — sensitivity-first, governance-reviewed</p>
                </div>
              </div>

              <div className="table-container">
                <table className="table">
                  <thead>
                    <tr>
                      <th>Rule Name</th>
                      <th>Severity</th>
                      <th>Category</th>
                      <th>SLA</th>
                      <th>Conditions</th>
                      <th>Status</th>
                    </tr>
                  </thead>
                  <tbody>
                    {rules.map((rule: any) => (
                      <tr key={rule.id}>
                        <td>
                          <div className="font-medium">{rule.name}</div>
                          <div className="text-xs text-muted">{rule.description}</div>
                        </td>
                        <td>
                          <span className={`badge ${rule.severity === 'critical' ? 'badge--danger' : rule.severity === 'high' ? 'badge--warning' : 'badge--primary'}`}>
                            {rule.severity}
                          </span>
                        </td>
                        <td><span className="badge badge--default">{rule.category}</span></td>
                        <td className="font-mono text-sm">{rule.slaMinutes}min</td>
                        <td className="text-xs">{rule.conditions?.length || 0} condition{rule.conditions?.length !== 1 ? 's' : ''} ({rule.logicOperator})</td>
                        <td>
                          <span className={`badge ${rule.active ? 'badge--success' : 'badge--default'}`}>
                            {rule.active ? 'Active' : 'Inactive'}
                          </span>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>

              <div className="alert alert--info" style={{ marginTop: '16px' }}>
                <span className="alert-icon">ℹ️</span>
                <div className="alert-content">
                  <div className="alert-title">Governance Notice</div>
                  Rule changes follow the pipeline: Propose → Clinical Review → Test Against Evaluation Dataset → Approval → Versioned Deployment → Monitored Rollout
                </div>
              </div>
            </>
          )}

          {/* Audit View */}
          {view === 'audit' && (
            <>
              <div className="page-header">
                <div>
                  <h1 className="page-title">Audit Trail</h1>
                  <p className="page-subtitle">Immutable, hash-chained log of all system events</p>
                </div>
              </div>

              <div className="table-container">
                <table className="table">
                  <thead>
                    <tr>
                      <th>Timestamp</th>
                      <th>Event</th>
                      <th>Actor</th>
                      <th>Resource</th>
                      <th>Details</th>
                    </tr>
                  </thead>
                  <tbody>
                    {auditEvents.map((event: any) => (
                      <tr key={event.id}>
                        <td className="font-mono text-xs">{new Date(event.timestamp).toLocaleString()}</td>
                        <td>
                          <span className={`badge ${
                            event.eventType.includes('red_flag') ? 'badge--danger' :
                            event.eventType.includes('consent') ? 'badge--warning' :
                            event.eventType.includes('approved') ? 'badge--success' :
                            'badge--default'
                          }`}>
                            {event.eventType.replace(/_/g, ' ')}
                          </span>
                        </td>
                        <td className="text-sm">{event.actorId} <span className="text-muted">({event.actorRole})</span></td>
                        <td className="text-xs text-muted">{event.resourceType}: {event.resourceId.substring(0, 8)}...</td>
                        <td className="text-xs">{JSON.stringify(event.details).substring(0, 80)}{JSON.stringify(event.details).length > 80 ? '...' : ''}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>

              {auditEvents.length === 0 && !isLoading && (
                <div className="empty-state">
                  <div className="empty-state-icon">📜</div>
                  <div className="empty-state-title">No Audit Events</div>
                </div>
              )}
            </>
          )}
        </div>
      </main>
    </div>
  );
}
