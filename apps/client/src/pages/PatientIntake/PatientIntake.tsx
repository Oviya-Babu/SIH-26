import { useRef, useState } from 'react';
import { Activity, ArrowLeft, ArrowRight, BadgeCheck, Check, CheckCircle2, ChevronRight, CircleHelp, FileCheck2, Headphones, Languages, LockKeyhole, Mic, Paperclip, Pause, Play, ScanLine, ShieldCheck, Sparkles, Volume2, Waves, X } from 'lucide-react';
import { SUPPORTED_LANGUAGES } from '@medikiosk/shared';
import { api } from '../../services/api';
import './PatientIntake.css';

type Step = 'registration' | 'language' | 'consent' | 'conversation' | 'documents' | 'complete';
type Question = { id: string; step: string; prompt: string; hint: string; type: 'yes_no' | 'body_map' | 'severity' | 'single_select'; options?: string[] };
type Consent = { id: string; title: string; detail: string; required: boolean; enabled: boolean; icon: typeof ShieldCheck };

const questions: Question[] = [
  { id: 'q-cc', step: '1 of 4', prompt: 'What is troubling you today?', hint: 'Tap a place on the body, or tell us in your own words.', type: 'body_map' },
  { id: 'q-severity', step: '2 of 4', prompt: 'How strong is the discomfort?', hint: '1 means very little. 10 means the worst pain you can imagine.', type: 'severity' },
  { id: 'q-duration', step: '3 of 4', prompt: 'When did it start?', hint: 'Choose the answer that feels closest.', type: 'single_select', options: ['Today', '2–3 days ago', 'About a week ago', 'More than a month ago'] },
  { id: 'q-history', step: '4 of 4', prompt: 'Do you have diabetes, blood pressure, or another long-term condition?', hint: 'It is okay to choose “I am not sure”.', type: 'yes_no' },
];

const initialConsents: Consent[] = [
  { id: 'intake_processing', title: 'Help me organise my answers', detail: 'MediKiosk will turn your answers into a short note for the doctor. A doctor will check it.', required: true, enabled: true, icon: Sparkles },
  { id: 'document_storage', title: 'Keep my reports safe', detail: 'Your uploaded prescriptions and reports can be stored securely for this visit.', required: true, enabled: true, icon: FileCheck2 },
  { id: 'his_integration', title: 'Add to my hospital record', detail: 'Add this visit to the hospital system after the doctor checks it.', required: false, enabled: false, icon: BadgeCheck },
  { id: 'abdm_sharing', title: 'Share through ABDM', detail: 'Share with your chosen health-record service for future care.', required: false, enabled: false, icon: LockKeyhole },
];

const languageLabels: Record<string, { native: string; spoken: string }> = {
  en: { native: 'English', spoken: 'English' }, hi: { native: 'हिन्दी', spoken: 'Hindi' }, ta: { native: 'தமிழ்', spoken: 'Tamil' }, te: { native: 'తెలుగు', spoken: 'Telugu' }, bn: { native: 'বাংলা', spoken: 'Bengali' }, mr: { native: 'मराठी', spoken: 'Marathi' }, kn: { native: 'ಕನ್ನಡ', spoken: 'Kannada' }, gu: { native: 'ગુજરાતી', spoken: 'Gujarati' }, ml: { native: 'മലയാളം', spoken: 'Malayalam' },
};

function now() { return new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }); }

export default function PatientIntake() {
  const [step, setStep] = useState<Step>('registration');
  const [language, setLanguage] = useState('en');
  const [patientForm, setPatientForm] = useState({ name: '', age: '', phone: '' });
  const [consents, setConsents] = useState(initialConsents);
  const [questionIndex, setQuestionIndex] = useState(0);
  const [selected, setSelected] = useState<string | number | string[] | null>(null);
  const [answers, setAnswers] = useState<Array<{ prompt: string; answer: string }>>([]);
  const [isRecording, setIsRecording] = useState(false);
  const [files, setFiles] = useState<string[]>([]);
  const [notice, setNotice] = useState('');
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);

  const currentQuestion = questions[questionIndex];
  const languageName = languageLabels[language]?.native || 'English';
  const completedStep = ({ registration: 1, language: 1, consent: 2, conversation: 3, documents: 4, complete: 5 } as Record<Step, number>)[step];

  const setTimedNotice = (message: string) => {
    setNotice(message);
    window.setTimeout(() => setNotice(''), 3200);
  };

  const startRegistration = async () => {
    setIsSubmitting(true);
    try {
      const response = await api.registerPatient({ firstName: patientForm.name || 'Demo Patient', lastName: '', age: Number(patientForm.age) || 45, sex: 'unspecified', phone: patientForm.phone, language });
      if (response?.data?.id) setSessionId(response.data.id);
    } catch { /* The kiosk remains usable for a local demonstration. */ }
    setIsSubmitting(false);
    setStep('language');
  };

  const startSession = async () => {
    setIsSubmitting(true);
    try {
      if (sessionId) {
        const response = await api.createSession({ patientId: sessionId, protocolId: 'general_medicine_v1', department: 'General Medicine', channel: 'kiosk', language });
        if (response?.data?.session?.id) setSessionId(response.data.session.id);
        await api.recordConsent({ sessionId: response?.data?.session?.id || sessionId, patientId: sessionId, consents: consents.map(({ id, enabled }) => ({ purpose: id, granted: enabled })) });
      }
    } catch { /* A network-free demo state is intentional for kiosk pilots. */ }
    setIsSubmitting(false);
    setStep('conversation');
  };

  const chooseLanguage = (code: string) => { setLanguage(code); setStep('consent'); };

  const toggleConsent = (id: string) => setConsents((items) => items.map((item) => item.id === id ? { ...item, enabled: !item.enabled } : item));

  const answerText = (value: string | number | string[]) => {
    if (Array.isArray(value)) return value.join(', ');
    if (typeof value === 'number') return `${value} out of 10`;
    return value;
  };

  const submitAnswer = async () => {
    if (selected === null) return;
    const display = answerText(selected);
    setAnswers((items) => [...items, { prompt: currentQuestion.prompt, answer: display }]);
    if (sessionId) {
      try { await api.submitAnswer(sessionId, { questionId: currentQuestion.id, value: selected, inputMethod: isRecording ? 'voice' : 'touch', idempotencyKey: `${currentQuestion.id}-${Date.now()}` }); } catch { /* Preserve the local patient flow if the server is not running. */ }
    }
    setSelected(null);
    if (questionIndex === questions.length - 1) setStep('documents'); else setQuestionIndex((index) => index + 1);
  };

  const toggleVoice = () => {
    setIsRecording((recording) => !recording);
    if (isRecording) { setSelected(currentQuestion.type === 'severity' ? 7 : currentQuestion.type === 'body_map' ? ['chest_left'] : currentQuestion.type === 'yes_no' ? 'Yes' : 'Today'); setTimedNotice('We heard you. Please check the answer, then tap Continue.'); }
  };

  const addFiles = (event: React.ChangeEvent<HTMLInputElement>) => {
    const names = Array.from(event.target.files || []).map((file) => file.name);
    setFiles((items) => [...items, ...names]);
    if (names.length) setTimedNotice(`${names.length} document${names.length > 1 ? 's' : ''} added`);
  };

  const finish = async () => {
    if (sessionId) { try { await api.generateSummary(sessionId); } catch { /* demo-safe */ } }
    setStep('complete');
  };

  const renderQuestionWidget = () => {
    if (currentQuestion.type === 'yes_no') return <div className="kiosk-choice-grid"><button className={`kiosk-choice kiosk-choice--yes ${selected === 'Yes' ? 'is-selected' : ''}`} onClick={() => setSelected('Yes')}><span className="choice-visual">✓</span><span><strong>Yes</strong><small>हाँ / ஆம்</small></span></button><button className={`kiosk-choice kiosk-choice--no ${selected === 'No' ? 'is-selected' : ''}`} onClick={() => setSelected('No')}><span className="choice-visual">×</span><span><strong>No</strong><small>नहीं / இல்லை</small></span></button><button className={`kiosk-choice kiosk-choice--unknown ${selected === 'Not sure' ? 'is-selected' : ''}`} onClick={() => setSelected('Not sure')}><span className="choice-visual"><CircleHelp size={28} /></span><span><strong>Not sure</strong><small>पता नहीं / தெரியாது</small></span></button></div>;
    if (currentQuestion.type === 'severity') {
      const value = typeof selected === 'number' ? selected : 5;
      return <div className="severity-widget"><div className="severity-face-row">{['😊', '🙂', '😐', '😕', '😣', '😫', '🤯'].map((face, index) => <button key={face} className={value === Math.max(1, Math.round((index + 1) * 1.5)) ? 'is-selected' : ''} onClick={() => setSelected(Math.max(1, Math.round((index + 1) * 1.5)))} aria-label={`Pain ${Math.max(1, Math.round((index + 1) * 1.5))} out of 10`}>{face}</button>)}</div><input aria-label="Discomfort from 1 to 10" className="severity-range" type="range" min="1" max="10" value={value} onChange={(event) => setSelected(Number(event.target.value))} /><div className="severity-scale"><span>Little</span><strong>{value} / 10</strong><span>Very strong</span></div></div>;
    }
    if (currentQuestion.type === 'single_select') return <div className="kiosk-option-grid">{currentQuestion.options?.map((option) => <button key={option} className={`kiosk-option ${selected === option ? 'is-selected' : ''}`} onClick={() => setSelected(option)}><span className="option-radio" />{option}<ChevronRight size={18} /></button>)}</div>;
    const zones = [{ id: 'head', label: 'Head', icon: '◯' }, { id: 'chest_left', label: 'Chest / heart', icon: '♡' }, { id: 'abdomen', label: 'Stomach', icon: '⬡' }, { id: 'left_arm', label: 'Left arm', icon: '╱' }, { id: 'right_arm', label: 'Right arm', icon: '╲' }, { id: 'left_leg', label: 'Left leg', icon: '╱' }, { id: 'right_leg', label: 'Right leg', icon: '╲' }, { id: 'back', label: 'Back', icon: '▣' }];
    const selectedZones = Array.isArray(selected) ? selected : [];
    return <div className="body-map-widget"><div className="body-map-figure" aria-label="Tap the body area that hurts"><div className="figure-head" /><div className="figure-body"><span className="figure-heart">♡</span></div><div className="figure-limb figure-limb--left-arm" /><div className="figure-limb figure-limb--right-arm" /><div className="figure-limb figure-limb--left-leg" /><div className="figure-limb figure-limb--right-leg" /></div><div className="body-zone-grid">{zones.map((zone) => <button key={zone.id} className={selectedZones.includes(zone.id) ? 'is-selected' : ''} onClick={() => setSelected(selectedZones.includes(zone.id) ? selectedZones.filter((item) => item !== zone.id) : [...selectedZones, zone.id])}><span>{zone.icon}</span>{zone.label}</button>)}</div></div>;
  };

  if (step === 'complete') return <div className="kiosk-shell kiosk-shell--complete"><header className="kiosk-topbar"><div className="kiosk-brand"><span className="kiosk-brand-mark"><Waves size={24} /></span><span>MediKiosk</span></div><span className="kiosk-secure"><LockKeyhole size={15} /> Your answers are private</span></header><main className="complete-card"><div className="complete-icon"><CheckCircle2 size={56} /></div><p className="kiosk-kicker">Check-in complete</p><h1>You are ready for the doctor.</h1><p className="complete-lede">Please keep this token. A staff member will call your number. You do not need to fill anything again.</p><div className="token-card"><div><span className="token-label">Your OPD token</span><strong>A-042</strong><span className="token-meta">General Medicine · Room 03</span></div><div className="token-qr" aria-label="Token QR code"><span /><span /><span /><span /><span /><span /><span /><span /><span /></div></div><div className="wait-row"><div><span>Estimated wait</span><strong>18 minutes</strong></div><div><span>Documents</span><strong>{files.length ? `${files.length} added` : 'None added'}</strong></div><div><span>Language</span><strong>{languageName}</strong></div></div><button className="kiosk-primary-button" onClick={() => window.location.reload()}>Finish</button><p className="complete-help"><Headphones size={16} /> Need help? Please ask the kiosk attendant.</p></main></div>;

  return <div className={`kiosk-shell ${step === 'conversation' ? 'kiosk-shell--conversation' : ''}`}>
    <header className="kiosk-topbar"><div className="kiosk-brand"><span className="kiosk-brand-mark"><Waves size={24} /></span><span>MediKiosk</span></div><div className="kiosk-topbar-right"><span className="kiosk-lang-chip"><Languages size={16} /> {languageName}</span><span className="kiosk-secure"><LockKeyhole size={15} /> Private & secure</span></div></header>
    {step === 'registration' && <main className="kiosk-welcome-layout"><div className="kiosk-welcome-copy"><p className="kiosk-kicker">Welcome to your care visit</p><h1>Let us make your doctor visit <span>easier.</span></h1><p>We will ask a few simple questions. You can <strong>speak</strong>, <strong>tap</strong>, or ask a family member to help.</p><div className="welcome-promise"><span><Mic size={18} /> Speak in your language</span><span><ShieldCheck size={18} /> Doctor checks every summary</span><span><Activity size={18} /> Usually takes 2–3 minutes</span></div></div><div className="kiosk-form-card"><div className="form-card-top"><span className="step-badge">Step 1 of 4</span><span className="form-card-time"><Activity size={14} /> 2–3 min</span></div><h2>Tell us about you</h2><p className="form-help">This helps the care team call you by the right name.</p><label>Your name<input value={patientForm.name} onChange={(event) => setPatientForm({ ...patientForm, name: event.target.value })} placeholder="Type your name" autoComplete="name" /></label><div className="form-row"><label>Age<input type="number" min="1" max="120" value={patientForm.age} onChange={(event) => setPatientForm({ ...patientForm, age: event.target.value })} placeholder="Age" /></label><label>Mobile <span className="optional-label">(optional)</span><input inputMode="tel" value={patientForm.phone} onChange={(event) => setPatientForm({ ...patientForm, phone: event.target.value })} placeholder="10-digit number" /></label></div><button className="kiosk-primary-button" onClick={startRegistration} disabled={isSubmitting}>Continue <ArrowRight size={20} /></button><p className="kiosk-form-note"><CircleHelp size={15} /> You can skip any question you do not understand.</p></div></main>}
    {step === 'language' && <main className="kiosk-centered-layout"><div className="kiosk-centered-heading"><span className="kiosk-round-icon"><Languages size={30} /></span><p className="kiosk-kicker">Step 2 of 4</p><h1>Which language feels easiest?</h1><p>We will speak and show questions in this language.</p></div><div className="language-grid">{Object.entries(SUPPORTED_LANGUAGES).map(([code, name]) => <button key={code} className={`language-card ${language === code ? 'is-selected' : ''}`} onClick={() => chooseLanguage(code)}><strong>{languageLabels[code]?.native || name}</strong><span>{languageLabels[code]?.spoken || name}</span><Check className="language-check" size={20} /></button>)}</div><p className="kiosk-centered-note"><Volume2 size={16} /> Audio guidance is available on every screen</p></main>}
    {step === 'consent' && <main className="kiosk-consent-layout"><div className="consent-intro"><span className="kiosk-round-icon"><ShieldCheck size={30} /></span><p className="kiosk-kicker">Step 3 of 4 · Your choice</p><h1>You are in control of your health information.</h1><p>Listen to each choice. Turn on only what you are comfortable with. The first two are needed to continue.</p><button className="listen-button" onClick={() => setTimedNotice('Audio guide playing in English')}><Play size={16} fill="currentColor" /> Listen to this page</button></div><div className="consent-list">{consents.map((item) => { const Icon = item.icon; return <button key={item.id} className={`consent-card ${item.enabled ? 'is-enabled' : ''}`} onClick={() => toggleConsent(item.id)}><span className="consent-icon"><Icon size={21} /></span><span className="consent-copy"><strong>{item.title}</strong><small>{item.detail}</small>{item.required && <em>Required for check-in</em>}</span><span className={`toggle ${item.enabled ? 'is-on' : ''}`}><span /></span></button>; })}<button className="kiosk-primary-button" onClick={startSession} disabled={isSubmitting || consents.some((item) => item.required && !item.enabled)}>I understand & continue <ArrowRight size={20} /></button><p className="consent-legal"><LockKeyhole size={14} /> Information is used only for your care at this hospital.</p></div></main>}
    {step === 'conversation' && <main className="conversation-layout"><aside className="conversation-sidebar"><div className="conversation-sidebar-heading"><span className="step-badge">Health check</span><strong>{questionIndex + 1}<small>/{questions.length}</small></strong><span>questions</span></div><div className="conversation-progress"><span style={{ width: `${((questionIndex + 1) / questions.length) * 100}%` }} /></div><div className="conversation-history">{answers.map((answer, index) => <div className="history-item" key={`${answer.prompt}-${index}`}><span className="history-check"><Check size={13} /></span><div><small>{answer.prompt}</small><strong>{answer.answer}</strong></div></div>)}<div className="history-item history-item--active"><span className="history-dot" /><div><small>Now</small><strong>Answer this question</strong></div></div></div><div className="conversation-sidebar-help"><Headphones size={17} /><span>Need help?<strong>Ask the attendant</strong></span></div></aside><section className="conversation-main"><div className="conversation-question"><div className="question-avatar"><Sparkles size={21} /></div><div><span className="question-step">Question {questionIndex + 1} · {currentQuestion.step}</span><h1>{currentQuestion.prompt}</h1><p>{currentQuestion.hint}</p></div><button className="speak-question" onClick={() => setTimedNotice('Question audio playing')} aria-label="Play question audio"><Volume2 size={20} /></button></div><div className="conversation-widget">{renderQuestionWidget()}</div><div className="voice-bar"><button className={`voice-button ${isRecording ? 'is-recording' : ''}`} onClick={toggleVoice} aria-label={isRecording ? 'Stop listening' : 'Speak your answer'}>{isRecording ? <Pause size={24} fill="currentColor" /> : <Mic size={24} />}</button><div className="voice-copy"><strong>{isRecording ? 'Listening…' : 'Prefer to speak?'}</strong><span>{isRecording ? 'Say your answer clearly, then tap the mic again.' : 'Tap the microphone and say your answer in your language.'}</span></div><div className="waveform" aria-hidden="true">{Array.from({ length: 18 }).map((_, index) => <i key={index} className={isRecording ? 'is-live' : ''} style={{ height: `${8 + ((index * 7) % 17)}px` }} />)}</div></div><div className="conversation-actions"><button className="back-text-button" onClick={() => questionIndex > 0 ? setQuestionIndex((index) => index - 1) : setStep('consent')}><ArrowLeft size={17} /> Back</button><button className="kiosk-primary-button" onClick={submitAnswer} disabled={selected === null}>Continue <ArrowRight size={20} /></button></div></section></main>}
    {step === 'documents' && <main className="documents-layout"><div className="documents-copy"><span className="kiosk-round-icon"><Paperclip size={29} /></span><p className="kiosk-kicker">Almost done · Step 4 of 4</p><h1>Do you have a report or prescription?</h1><p>A photo is enough. We will organise it for the doctor — you do not need to read it.</p><div className="document-pipeline"><span className="pipeline-active"><ScanLine size={18} /> Take a photo</span><ChevronRight size={17} /><span><Sparkles size={18} /> Read the document</span><ChevronRight size={17} /><span><FileCheck2 size={18} /> Show the doctor</span></div></div><div className="upload-card"><div className="upload-visual"><ScanLine size={40} /><span>Place your paper inside the frame</span></div><input ref={fileInput} type="file" accept="image/*,.pdf" multiple onChange={addFiles} hidden /><button className="upload-button" onClick={() => fileInput.current?.click()}><Paperclip size={20} /> Choose photo or PDF</button><button className="camera-button" onClick={() => setTimedNotice('Camera preview is ready for the kiosk device')}><ScanLine size={20} /> Use camera</button>{files.length > 0 && <div className="file-list">{files.map((file) => <div key={file}><FileCheck2 size={16} /><span>{file}</span><button onClick={() => setFiles((items) => items.filter((item) => item !== file))} aria-label={`Remove ${file}`}><X size={15} /></button></div>)}</div>}<div className="ocr-status"><span className="status-pulse" /><span><strong>Secure document processing</strong><small>Only your care team can see this</small></span></div><button className="kiosk-primary-button" onClick={finish}>Continue to token <ArrowRight size={20} /></button><button className="skip-button" onClick={finish}>Skip for now</button></div></main>}
    {notice && <div className="kiosk-toast" role="status"><CheckCircle2 size={18} />{notice}</div>}
  </div>;
}
