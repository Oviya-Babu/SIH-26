import type { AppRole } from '../../App';
import './Landing.css';

interface Props {
  onSelectRole: (role: AppRole) => void;
}

export default function Landing({ onSelectRole }: Props) {
  const roles = [
    {
      id: 'patient' as AppRole,
      icon: '🏥',
      title: 'Patient Intake',
      subtitle: 'Start your pre-consultation check-in',
      description: 'Record your medical history through guided conversation, upload documents, and prepare for your doctor visit.',
      gradient: 'linear-gradient(135deg, #3b82f6, #06b6d4)',
    },
    {
      id: 'physician' as AppRole,
      icon: '👨‍⚕️',
      title: 'Physician Dashboard',
      subtitle: 'Review patient summaries & records',
      description: 'View AI-drafted clinical summaries, review evidence, verify facts, and approve records before consultation.',
      gradient: 'linear-gradient(135deg, #10b981, #059669)',
    },
    {
      id: 'nurse' as AppRole,
      icon: '🩺',
      title: 'Staff Console',
      subtitle: 'Triage alerts & session monitoring',
      description: 'Monitor active intake sessions, respond to red-flag alerts, and manage patient queue in real-time.',
      gradient: 'linear-gradient(135deg, #f59e0b, #d97706)',
    },
    {
      id: 'hospital_admin' as AppRole,
      icon: '📊',
      title: 'Admin Panel',
      subtitle: 'Analytics, governance & audit',
      description: 'View operational metrics, manage clinical protocols, review red-flag rules, and access audit trails.',
      gradient: 'linear-gradient(135deg, #8b5cf6, #6d28d9)',
    },
  ];

  return (
    <div className="landing-page">
      <div className="landing-bg-orbs">
        <div className="landing-orb landing-orb--1" />
        <div className="landing-orb landing-orb--2" />
        <div className="landing-orb landing-orb--3" />
      </div>

      <div className="landing-content">
        <div className="landing-header">
          <div className="landing-logo">
            <div className="landing-logo-icon">
              <span>M</span>
            </div>
            <div>
              <h1 className="landing-title">
                Medi<span>Kiosk</span>
              </h1>
              <p className="landing-tagline">
                AI-Powered Pre-Consultation Clinical Intake Platform
              </p>
            </div>
          </div>
          <p className="landing-description">
            Structured, evidence-backed, physician-verifiable clinical context — before the consultation begins.
          </p>
        </div>

        <div className="landing-roles">
          {roles.map((role) => (
            <button
              key={role.id}
              id={`role-select-${role.id}`}
              className="landing-role-card"
              onClick={() => onSelectRole(role.id)}
            >
              <div className="landing-role-icon" style={{ background: role.gradient }}>
                <span>{role.icon}</span>
              </div>
              <div className="landing-role-info">
                <h2 className="landing-role-title">{role.title}</h2>
                <p className="landing-role-subtitle">{role.subtitle}</p>
                <p className="landing-role-description">{role.description}</p>
              </div>
              <div className="landing-role-arrow">→</div>
            </button>
          ))}
        </div>

        <div className="landing-footer">
          <div className="landing-footer-badges">
            <span className="badge badge--primary">
              <span className="badge-dot" />
              Protocol-Governed
            </span>
            <span className="badge badge--success">
              <span className="badge-dot" />
              Evidence-Backed
            </span>
            <span className="badge badge--warning">
              <span className="badge-dot" />
              Physician-Verified
            </span>
          </div>
          <p className="text-muted text-xs" style={{ marginTop: '12px' }}>
            AI-assisted clinical intake — not autonomous healthcare. Every fact is traceable, every summary is a draft until physician approval.
          </p>
        </div>
      </div>
    </div>
  );
}
