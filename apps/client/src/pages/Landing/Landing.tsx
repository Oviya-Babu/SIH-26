import type { AppRole } from '../../App';
import { Activity, ArrowUpRight, CheckCircle2, ClipboardCheck, HeartPulse, Languages, LockKeyhole, ShieldCheck, Stethoscope, UsersRound, Waves } from 'lucide-react';
import './Landing.css';

interface Props {
  onSelectRole: (role: AppRole) => void;
}

type RoleCard = {
  id: AppRole;
  eyebrow: string;
  title: string;
  description: string;
  cta: string;
  icon: typeof HeartPulse;
  tone: string;
};

const roleCards: RoleCard[] = [
  {
    id: 'patient',
    eyebrow: 'For patients & families',
    title: 'Patient check-in',
    description: 'Speak or tap through a simple, guided health check before you meet your doctor.',
    cta: 'Start check-in',
    icon: HeartPulse,
    tone: 'patient',
  },
  {
    id: 'physician',
    eyebrow: 'For doctors',
    title: 'Clinical cockpit',
    description: 'See the right context first, verify AI-drafted summaries, and stay in control.',
    cta: 'Open cockpit',
    icon: Stethoscope,
    tone: 'physician',
  },
  {
    id: 'nurse',
    eyebrow: 'For triage teams',
    title: 'Staff console',
    description: 'Catch red flags early, coordinate assistance, and keep every kiosk moving.',
    cta: 'Open console',
    icon: Activity,
    tone: 'staff',
  },
  {
    id: 'hospital_admin',
    eyebrow: 'For hospital leaders',
    title: 'Governance hub',
    description: 'Track throughput, manage protocols, and inspect a tamper-evident audit trail.',
    cta: 'View governance',
    icon: ClipboardCheck,
    tone: 'admin',
  },
];

export default function Landing({ onSelectRole }: Props) {
  return (
    <main className="landing-shell">
      <div className="landing-grid-lines" aria-hidden="true" />
      <div className="landing-glow landing-glow--cyan" aria-hidden="true" />
      <div className="landing-glow landing-glow--indigo" aria-hidden="true" />

      <header className="landing-topbar">
        <div className="brand-lockup">
          <div className="brand-mark" aria-hidden="true"><Waves size={22} strokeWidth={2.4} /></div>
          <div>
            <div className="brand-name">Medi<span>Kiosk</span></div>
            <div className="brand-caption">Clinical context, made human</div>
          </div>
        </div>
        <div className="landing-topbar-meta">
          <span className="live-chip"><span className="live-dot" /> Live care network</span>
          <span className="topbar-divider" />
          <span className="topbar-date">23 Aug 2026</span>
        </div>
      </header>

      <section className="landing-hero">
        <div className="landing-hero-copy">
          <div className="eyebrow"><span className="eyebrow-line" /> Pre-consultation intelligence platform</div>
          <h1>A clearer start to every <em>clinical visit.</em></h1>
          <p className="landing-lede">MediKiosk turns a few minutes of patient voice, touch and documents into structured context that helps care teams act with confidence.</p>
          <div className="landing-trust-row">
            <span><ShieldCheck size={16} /> Physician verified</span>
            <span><LockKeyhole size={16} /> DPDP ready</span>
            <span><Languages size={16} /> Voice-first</span>
          </div>
        </div>

        <div className="landing-hero-visual" aria-label="MediKiosk care context overview">
          <div className="hero-orbit hero-orbit--outer" />
          <div className="hero-orbit hero-orbit--inner" />
          <div className="hero-pulse-card hero-pulse-card--top"><span className="pulse-icon pulse-icon--green"><CheckCircle2 size={15} /></span><span><strong>Protocol aligned</strong><small>General Medicine v1</small></span></div>
          <div className="hero-pulse-card hero-pulse-card--bottom"><span className="pulse-icon pulse-icon--blue"><UsersRound size={15} /></span><span><strong>One connected record</strong><small>Patient → staff → physician</small></span></div>
          <div className="hero-core">
            <div className="hero-core-ring"><HeartPulse size={42} strokeWidth={1.7} /></div>
            <span className="hero-core-label">Care context</span>
            <span className="hero-core-value">+ 1 clear next step</span>
          </div>
        </div>
      </section>

      <section className="landing-workspace-section" aria-labelledby="workspace-title">
        <div className="section-heading-row">
          <div>
            <p className="section-kicker">Choose your workspace</p>
            <h2 id="workspace-title">Start where you are.</h2>
          </div>
          <p className="section-helper">Every role sees only what they need — with the same trusted clinical record underneath.</p>
        </div>
        <div className="role-card-grid">
          {roleCards.map((role) => {
            const Icon = role.icon;
            return (
              <button key={role.id} className={`role-card role-card--${role.tone}`} onClick={() => onSelectRole(role.id)}>
                <span className="role-card-icon"><Icon size={22} strokeWidth={2} /></span>
                <span className="role-card-content">
                  <span className="role-card-eyebrow">{role.eyebrow}</span>
                  <span className="role-card-title">{role.title}</span>
                  <span className="role-card-description">{role.description}</span>
                </span>
                <span className="role-card-cta">{role.cta}<ArrowUpRight size={16} /></span>
              </button>
            );
          })}
        </div>
      </section>

      <section className="landing-metrics" aria-label="Network metrics">
        <div className="metric-cell"><strong>12,480<span>+</span></strong><span>patients screened today</span></div>
        <div className="metric-cell"><strong>2<span>m</span> 18<span>s</span></strong><span>median intake time</span></div>
        <div className="metric-cell"><strong>99.4<span>%</span></strong><span>protocol adherence</span></div>
        <div className="metric-cell metric-cell--badge"><span className="abdm-seal">A</span><span><strong>ABDM ready</strong><small>Consent-led data exchange</small></span></div>
      </section>

      <footer className="landing-footer">
        <span>AI-assisted clinical intake — <strong>not autonomous diagnosis.</strong> Every summary remains a draft until a physician signs off.</span>
        <span className="landing-footer-right"><span className="footer-status-dot" /> All systems operational</span>
      </footer>
    </main>
  );
}
