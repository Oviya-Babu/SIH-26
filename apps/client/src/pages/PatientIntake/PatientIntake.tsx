import { useState, useEffect, useRef } from 'react';
import { SUPPORTED_LANGUAGES } from '@medikiosk/shared';
import { api } from '../../services/api';
import './PatientIntake.css';

type IntakeStep = 'registration' | 'language' | 'consent' | 'conversation' | 'documents' | 'complete';

interface ConsentItem {
  purpose: string;
  title: string;
  description: string;
  granted: boolean;
  required: boolean;
}

export default function PatientIntake() {
  const [step, setStep] = useState<IntakeStep>('registration');
  const [language, setLanguage] = useState('en');
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [patientId, setPatientId] = useState<string | null>(null);
  const [currentQuestion, setCurrentQuestion] = useState<any>(null);
  const [progress, setProgress] = useState<any>(null);
  const [chatHistory, setChatHistory] = useState<Array<{ type: 'system' | 'patient'; text: string; time: string }>>([]);
  const [isRecording, setIsRecording] = useState(false);
  const [selectedValues, setSelectedValues] = useState<any>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [redFlagPaused, setRedFlagPaused] = useState(false);
  const [redFlagMessage, setRedFlagMessage] = useState('');
  const [confirmBack, setConfirmBack] = useState<{ text: string; questionId: string } | null>(null);
  const [patientForm, setPatientForm] = useState({ firstName: '', lastName: '', age: '', sex: 'male' as string, phone: '' });
  const [consents, setConsents] = useState<ConsentItem[]>([
    { purpose: 'intake_processing', title: 'AI Processing Consent', description: 'AI will help understand and structure your responses. All information will be verified by the physician.', granted: false, required: true },
    { purpose: 'document_storage', title: 'Document Storage', description: 'Your uploaded medical documents will be securely stored and processed.', granted: false, required: true },
    { purpose: 'his_integration', title: 'Hospital Record Integration', description: 'Your information will be integrated with the hospital electronic health records after physician approval.', granted: false, required: false },
    { purpose: 'abdm_sharing', title: 'ABDM Health Record Sharing', description: 'Share your health records via Ayushman Bharat Digital Mission for continuity of care.', granted: false, required: false },
  ]);
  const chatEndRef = useRef<HTMLDivElement>(null);
  const waveformBars = useRef<number[]>(Array(20).fill(4));

  useEffect(() => {
    chatEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [chatHistory]);

  // Simulate voice waveform animation
  useEffect(() => {
    if (!isRecording) return;
    const interval = setInterval(() => {
      waveformBars.current = waveformBars.current.map(() => Math.random() * 35 + 5);
    }, 100);
    return () => clearInterval(interval);
  }, [isRecording]);

  const handleRegistration = async () => {
    setIsLoading(true);
    try {
      const res = await api.registerPatient({
        firstName: patientForm.firstName || 'Demo',
        lastName: patientForm.lastName || 'Patient',
        age: parseInt(patientForm.age) || 45,
        sex: patientForm.sex,
        phone: patientForm.phone,
        language,
      });
      setPatientId(res.data.id);
      setStep('language');
    } catch (e) {
      console.error(e);
    }
    setIsLoading(false);
  };

  const handleLanguageSelect = (lang: string) => {
    setLanguage(lang);
    setStep('consent');
  };

  const handleConsentSubmit = async (consents: ConsentItem[]) => {
    if (!patientId) return;
    setIsLoading(true);
    try {
      // Create session
      const sessionRes = await api.createSession({
        patientId,
        protocolId: 'general_medicine_v1',
        department: 'General Medicine',
        channel: 'kiosk',
        language,
      });

      const newSessionId = sessionRes.data.session.id;
      setSessionId(newSessionId);

      // Record consents
      await api.recordConsent({
        sessionId: newSessionId,
        patientId,
        consents: consents.map(c => ({ purpose: c.purpose, granted: c.granted })),
      });

      // Set first question
      if (sessionRes.data.nextQuestion) {
        setCurrentQuestion(sessionRes.data.nextQuestion.question);
        setProgress(sessionRes.data.nextQuestion.progress);
        setChatHistory([{
          type: 'system',
          text: sessionRes.data.nextQuestion.question.voicePrompt,
          time: new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }),
        }]);
      }

      setStep('conversation');
    } catch (e) {
      console.error(e);
    }
    setIsLoading(false);
  };

  const handleAnswer = async (value: any, displayText?: string) => {
    if (!sessionId || !currentQuestion) return;
    setIsLoading(true);

    // Add patient's answer to chat
    const answerText = displayText || (Array.isArray(value) ? value.join(', ') : String(value));
    setChatHistory(prev => [...prev, {
      type: 'patient',
      text: answerText,
      time: new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }),
    }]);

    try {
      const res = await api.submitAnswer(sessionId, {
        questionId: currentQuestion.id,
        value,
        inputMethod: 'touch',
        idempotencyKey: `${sessionId}-${currentQuestion.id}-${Date.now()}`,
      });

      const data = res.data;

      // Check for red flag
      if (data.redFlagFired && data.redFlagAlert) {
        setRedFlagPaused(true);
        setRedFlagMessage(data.redFlagAlert.patientMessage);
        setChatHistory(prev => [...prev, {
          type: 'system',
          text: data.redFlagAlert.patientMessage,
          time: new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }),
        }]);
      }

      // Check for confirm-back
      if (data.confirmBack) {
        setConfirmBack({
          text: data.confirmBack.displayText,
          questionId: data.confirmBack.questionId,
        });
      }

      // Update progress
      if (data.nextQuestion) {
        setCurrentQuestion(data.nextQuestion.question);
        setProgress(data.nextQuestion.progress);

        if (!data.redFlagFired) {
          setChatHistory(prev => [...prev, {
            type: 'system',
            text: data.nextQuestion.question.voicePrompt,
            time: new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }),
          }]);
        }
      } else if (data.sessionStatus === 'intake_complete') {
        setStep('documents');
      }

      setSelectedValues(null);
    } catch (e) {
      console.error(e);
    }
    setIsLoading(false);
  };

  const handleVoiceToggle = () => {
    if (isRecording) {
      setIsRecording(false);
      // Simulate voice recognition result
      setTimeout(() => {
        const mockTranscriptions: Record<string, string> = {
          'q-cc': 'chest pain',
          'q-onset': 'since this morning',
          'q-location': 'left side of chest',
          'q-severity': '7',
        };
        const result = mockTranscriptions[currentQuestion?.id] || 'yes';
        handleAnswer(result, `🎤 "${result}"`);
      }, 500);
    } else {
      setIsRecording(true);
    }
  };

  const goToComplete = async () => {
    if (sessionId) {
      try {
        await api.generateSummary(sessionId);
      } catch (e) {
        console.error(e);
      }
    }
    setStep('complete');
  };

  // ─── Render Helpers ──────────────────────────────────────────────────────

  const renderWidget = () => {
    if (!currentQuestion || redFlagPaused) return null;

    switch (currentQuestion.widgetType) {
      case 'yes_no':
        return (
          <div className="widget-yesno">
            <button className="btn btn--lg btn--success" onClick={() => handleAnswer(true, 'Yes ✓')} disabled={isLoading}>
              {language === 'hi' ? 'हाँ' : 'Yes'}
            </button>
            <button className="btn btn--lg btn--secondary" onClick={() => handleAnswer(false, 'No ✗')} disabled={isLoading}>
              {language === 'hi' ? 'नहीं' : 'No'}
            </button>
            <button className="btn btn--lg btn--ghost" onClick={() => handleAnswer('unknown', "I don't know")} disabled={isLoading}>
              {language === 'hi' ? 'पता नहीं' : "Don't know"}
            </button>
          </div>
        );

      case 'severity_slider':
        return (
          <div className="severity-slider">
            <div className="severity-faces">
              {['😊', '🙂', '😐', '😕', '😟', '😣', '😖', '😫', '😩', '🤯'].map((face, i) => (
                <span
                  key={i}
                  className={`severity-face ${selectedValues === i + 1 ? 'severity-face--selected' : ''}`}
                  onClick={() => setSelectedValues(i + 1)}
                >
                  {face}
                </span>
              ))}
            </div>
            <input
              type="range" min="1" max="10"
              value={selectedValues || 5}
              onChange={(e) => setSelectedValues(parseInt(e.target.value))}
              className="severity-track"
            />
            <div className="flex justify-between text-sm text-muted" style={{ padding: '0 4px' }}>
              <span>{language === 'hi' ? 'हल्का' : 'Mild'}</span>
              <span className="font-bold text-xl">{selectedValues || 5}/10</span>
              <span>{language === 'hi' ? 'बहुत तेज़' : 'Worst'}</span>
            </div>
            <button
              className="btn btn--primary btn--lg btn--full"
              onClick={() => handleAnswer(selectedValues || 5, `Severity: ${selectedValues || 5}/10`)}
              disabled={isLoading}
              style={{ marginTop: '12px' }}
            >
              {language === 'hi' ? 'जारी रखें' : 'Continue'}
            </button>
          </div>
        );

      case 'multi_select':
      case 'single_select':
        return (
          <div className="widget-select">
            <div className="widget-options">
              {currentQuestion.options?.map((opt: any) => {
                const isSelected = currentQuestion.widgetType === 'multi_select'
                  ? (selectedValues || []).includes(opt.value)
                  : selectedValues === opt.value;
                return (
                  <button
                    key={opt.value}
                    className={`widget-option ${isSelected ? 'widget-option--selected' : ''}`}
                    onClick={() => {
                      if (currentQuestion.widgetType === 'multi_select') {
                        const current = selectedValues || [];
                        setSelectedValues(
                          current.includes(opt.value)
                            ? current.filter((v: string) => v !== opt.value)
                            : [...current, opt.value]
                        );
                      } else {
                        setSelectedValues(opt.value);
                      }
                    }}
                  >
                    <span className={`widget-option-check ${isSelected ? 'widget-option-check--active' : ''}`}>
                      {isSelected ? '✓' : ''}
                    </span>
                    {opt.label}
                  </button>
                );
              })}
            </div>
            <button
              className="btn btn--primary btn--lg btn--full"
              onClick={() => {
                const val = currentQuestion.widgetType === 'multi_select' ? (selectedValues || []) : selectedValues;
                if (val && (Array.isArray(val) ? val.length > 0 : true)) {
                  const display = Array.isArray(val)
                    ? val.map((v: string) => currentQuestion.options?.find((o: any) => o.value === v)?.label || v).join(', ')
                    : currentQuestion.options?.find((o: any) => o.value === val)?.label || val;
                  handleAnswer(val, display);
                }
              }}
              disabled={isLoading || !selectedValues || (Array.isArray(selectedValues) && selectedValues.length === 0)}
              style={{ marginTop: '16px' }}
            >
              {language === 'hi' ? 'जारी रखें' : 'Continue'}
            </button>
          </div>
        );

      case 'body_map':
        return (
          <div className="widget-bodymap">
            <div className="bodymap-grid">
              {['Head', 'Neck', 'Chest (Left)', 'Chest (Center)', 'Chest (Right)',
                'Upper Back', 'Abdomen', 'Lower Back', 'Left Arm', 'Right Arm',
                'Left Leg', 'Right Leg'].map((region) => {
                const value = region.toLowerCase().replace(/[() ]/g, '_');
                const isSelected = (selectedValues || []).includes(value);
                return (
                  <button
                    key={value}
                    className={`bodymap-region ${isSelected ? 'bodymap-region--selected' : ''}`}
                    onClick={() => {
                      const current = selectedValues || [];
                      setSelectedValues(
                        current.includes(value)
                          ? current.filter((v: string) => v !== value)
                          : [...current, value]
                      );
                    }}
                  >
                    <span className="bodymap-region-dot" />
                    {region}
                  </button>
                );
              })}
            </div>
            <button
              className="btn btn--primary btn--lg btn--full"
              onClick={() => {
                if (selectedValues?.length > 0) {
                  handleAnswer(selectedValues, `Location: ${selectedValues.join(', ')}`);
                }
              }}
              disabled={isLoading || !selectedValues?.length}
              style={{ marginTop: '16px' }}
            >
              {language === 'hi' ? 'जारी रखें' : 'Continue'}
            </button>
          </div>
        );

      case 'text_input':
        return (
          <div className="widget-text">
            <textarea
              className="input input--lg"
              placeholder={currentQuestion.helpText || (language === 'hi' ? 'यहाँ लिखें...' : 'Type your answer here...')}
              value={selectedValues || ''}
              onChange={(e) => setSelectedValues(e.target.value)}
              rows={3}
            />
            <button
              className="btn btn--primary btn--lg btn--full"
              onClick={() => {
                if (selectedValues?.trim()) {
                  handleAnswer(selectedValues.trim());
                }
              }}
              disabled={isLoading || !selectedValues?.trim()}
              style={{ marginTop: '12px' }}
            >
              {language === 'hi' ? 'जारी रखें' : 'Continue'}
            </button>
          </div>
        );

      case 'duration_picker':
        return (
          <div className="widget-duration">
            <div className="duration-grid">
              {['Today', 'Yesterday', '2-3 days', '1 week', '2 weeks', '1 month', '3 months', '6 months', '1 year', '2+ years'].map(dur => (
                <button
                  key={dur}
                  className={`widget-option ${selectedValues === dur ? 'widget-option--selected' : ''}`}
                  onClick={() => setSelectedValues(dur)}
                >
                  {dur}
                </button>
              ))}
            </div>
            <button
              className="btn btn--primary btn--lg btn--full"
              onClick={() => { if (selectedValues) handleAnswer(selectedValues, `Duration: ${selectedValues}`); }}
              disabled={isLoading || !selectedValues}
              style={{ marginTop: '16px' }}
            >
              {language === 'hi' ? 'जारी रखें' : 'Continue'}
            </button>
          </div>
        );

      default:
        return (
          <div className="widget-text">
            <input
              className="input input--lg"
              placeholder={language === 'hi' ? 'अपना जवाब लिखें' : 'Type your answer'}
              value={selectedValues || ''}
              onChange={(e) => setSelectedValues(e.target.value)}
            />
            <button
              className="btn btn--primary btn--lg btn--full"
              onClick={() => { if (selectedValues?.trim()) handleAnswer(selectedValues.trim()); }}
              disabled={isLoading || !selectedValues?.trim()}
              style={{ marginTop: '12px' }}
            >
              {language === 'hi' ? 'जारी रखें' : 'Continue'}
            </button>
          </div>
        );
    }
  };

  // ─── Registration Step ───────────────────────────────────────────────────

  if (step === 'registration') {
    return (
      <div className="intake-layout">
        <div className="intake-header">
          <div className="sidebar-logo">
            <div className="sidebar-logo-icon">M</div>
            <span className="sidebar-logo-text"><span>MediKiosk</span></span>
          </div>
        </div>
        <div className="intake-body">
          <div className="intake-question-card card" style={{ maxWidth: '500px' }}>
            <h2 className="text-2xl font-bold" style={{ marginBottom: '8px' }}>Welcome to MediKiosk</h2>
            <p className="text-secondary text-sm" style={{ marginBottom: '32px' }}>
              Let's start your pre-consultation check-in. Please enter your basic details.
            </p>
            <div style={{ display: 'flex', flexDirection: 'column', gap: '16px', textAlign: 'left' }}>
              <div className="input-group">
                <label className="input-label">First Name</label>
                <input className="input" placeholder="Enter first name" value={patientForm.firstName} onChange={e => setPatientForm(p => ({ ...p, firstName: e.target.value }))} />
              </div>
              <div className="input-group">
                <label className="input-label">Last Name</label>
                <input className="input" placeholder="Enter last name" value={patientForm.lastName} onChange={e => setPatientForm(p => ({ ...p, lastName: e.target.value }))} />
              </div>
              <div style={{ display: 'flex', gap: '16px' }}>
                <div className="input-group" style={{ flex: 1 }}>
                  <label className="input-label">Age</label>
                  <input className="input" type="number" placeholder="Age" value={patientForm.age} onChange={e => setPatientForm(p => ({ ...p, age: e.target.value }))} />
                </div>
                <div className="input-group" style={{ flex: 1 }}>
                  <label className="input-label">Sex</label>
                  <select className="input select" value={patientForm.sex} onChange={e => setPatientForm(p => ({ ...p, sex: e.target.value }))}>
                    <option value="male">Male</option>
                    <option value="female">Female</option>
                    <option value="other">Other</option>
                  </select>
                </div>
              </div>
              <div className="input-group">
                <label className="input-label">Phone (optional)</label>
                <input className="input" placeholder="+91-XXXXXXXXXX" value={patientForm.phone} onChange={e => setPatientForm(p => ({ ...p, phone: e.target.value }))} />
              </div>
            </div>
            <button className="btn btn--primary btn--lg btn--full" onClick={handleRegistration} disabled={isLoading} style={{ marginTop: '24px' }}>
              {isLoading ? <span className="spinner spinner--sm" /> : 'Continue →'}
            </button>
          </div>
        </div>
      </div>
    );
  }

  // ─── Language Selection ──────────────────────────────────────────────────

  if (step === 'language') {
    return (
      <div className="intake-layout">
        <div className="intake-header">
          <div className="sidebar-logo">
            <div className="sidebar-logo-icon">M</div>
            <span className="sidebar-logo-text"><span>MediKiosk</span></span>
          </div>
        </div>
        <div className="intake-body">
          <div style={{ textAlign: 'center', marginBottom: '40px' }}>
            <h2 className="text-3xl font-bold">Choose Your Language</h2>
            <p className="text-secondary" style={{ marginTop: '8px' }}>अपनी भाषा चुनें</p>
          </div>
          <div className="language-grid">
            {Object.entries(SUPPORTED_LANGUAGES).map(([code, name]) => (
              <button
                key={code}
                className={`language-card ${language === code ? 'language-card--selected' : ''}`}
                onClick={() => handleLanguageSelect(code)}
              >
                <span className="language-native">{name}</span>
                <span className="language-english">{code.toUpperCase()}</span>
              </button>
            ))}
          </div>
        </div>
      </div>
    );
  }

  // ─── Consent Step ────────────────────────────────────────────────────────

  if (step === 'consent') {
    const requiredMet = consents.filter(c => c.required).every(c => c.granted);

    return (
      <div className="intake-layout">
        <div className="intake-header">
          <div className="sidebar-logo">
            <div className="sidebar-logo-icon">M</div>
            <span className="sidebar-logo-text"><span>MediKiosk</span></span>
          </div>
          <span className="badge badge--primary badge--lg">
            {language === 'hi' ? 'सहमति' : 'Consent'}
          </span>
        </div>
        <div className="intake-body">
          <div className="consent-container">
            <h2 className="text-2xl font-bold" style={{ textAlign: 'center', marginBottom: '8px' }}>
              {language === 'hi' ? 'आपकी सहमति आवश्यक है' : 'Your Consent is Required'}
            </h2>
            <p className="text-secondary text-sm" style={{ textAlign: 'center', marginBottom: '32px' }}>
              {language === 'hi' ? 'कृपया निम्नलिखित को पढ़ें और अपनी सहमति दें' : 'Please review and provide your consent for each item below'}
            </p>

            {consents.map((consent, i) => (
              <div
                key={consent.purpose}
                className={`consent-item ${consent.granted ? 'consent-item--granted' : ''}`}
                onClick={() => {
                  const updated = [...consents];
                  updated[i] = { ...updated[i], granted: !updated[i].granted };
                  setConsents(updated);
                }}
              >
                <div className={`consent-checkbox ${consent.granted ? 'consent-checkbox--checked' : ''}`}>
                  {consent.granted && '✓'}
                </div>
                <div>
                  <div className="consent-title">
                    {consent.title}
                    {consent.required && <span className="text-danger text-xs" style={{ marginLeft: '8px' }}>Required</span>}
                  </div>
                  <div className="consent-description">{consent.description}</div>
                </div>
              </div>
            ))}

            <button
              className="btn btn--primary btn--xl btn--full"
              onClick={() => handleConsentSubmit(consents)}
              disabled={!requiredMet || isLoading}
              style={{ marginTop: '24px' }}
            >
              {isLoading ? <span className="spinner spinner--sm" /> : (language === 'hi' ? 'सहमत हूँ और आगे बढ़ें' : 'I Agree & Continue')}
            </button>
          </div>
        </div>
      </div>
    );
  }

  // ─── Conversation Step ───────────────────────────────────────────────────

  if (step === 'conversation') {
    return (
      <div className="intake-layout">
        <div className="intake-header">
          <div className="sidebar-logo">
            <div className="sidebar-logo-icon">M</div>
            <span className="sidebar-logo-text"><span>MediKiosk</span></span>
          </div>
          {progress && (
            <div className="intake-progress">
              <div className="flex justify-between text-xs" style={{ marginBottom: '4px' }}>
                <span className="text-muted">{progress.currentGroupLabel}</span>
                <span className="text-accent font-semibold">{Math.round(progress.completenessScore * 100)}%</span>
              </div>
              <div className="progress">
                <div className="progress-bar" style={{ width: `${progress.completenessScore * 100}%` }} />
              </div>
            </div>
          )}
          <span className="badge badge--primary badge--lg">
            {progress ? `${progress.answeredCount} / ${progress.totalRequired} questions` : 'Starting...'}
          </span>
        </div>

        <div className="intake-conversation">
          <div className="conversation-left">
            <div className="chat-container" style={{ flex: 1, maxHeight: 'none' }}>
              {chatHistory.map((msg, i) => (
                <div key={i} className={`chat-bubble chat-bubble--${msg.type}`} style={{ animationDelay: `${i * 0.05}s` }}>
                  {msg.text}
                  <div className="chat-bubble-time">{msg.time}</div>
                </div>
              ))}
              <div ref={chatEndRef} />
            </div>
          </div>

          <div className="conversation-right">
            {redFlagPaused ? (
              <div className="redflag-card redflag-card--critical" style={{ margin: '20px' }}>
                <div className="redflag-header">
                  <div className="redflag-severity">
                    <div className="redflag-severity-icon">⚠️</div>
                    <div className="redflag-title">{language === 'hi' ? 'कृपया प्रतीक्षा करें' : 'Please Wait'}</div>
                  </div>
                </div>
                <p className="redflag-details">{redFlagMessage}</p>
                <div className="spinner spinner--lg" style={{ margin: '20px auto' }} />
              </div>
            ) : currentQuestion ? (
              <div className="intake-question-card" style={{ padding: '24px' }}>
                <h3 className="intake-question-text">{currentQuestion.voicePrompt}</h3>
                {currentQuestion.helpText && (
                  <p className="text-muted text-sm" style={{ marginBottom: '20px' }}>{currentQuestion.helpText}</p>
                )}
                {renderWidget()}
              </div>
            ) : (
              <div className="empty-state">
                <div className="spinner spinner--lg" style={{ margin: '0 auto 16px' }} />
                <p className="text-secondary">Loading question...</p>
              </div>
            )}
          </div>
        </div>

        <div className="intake-footer">
          <button
            className={`voice-btn ${isRecording ? 'voice-btn--recording' : ''}`}
            onClick={handleVoiceToggle}
            disabled={isLoading || redFlagPaused}
          >
            {isRecording ? '⏹' : '🎤'}
          </button>
          <span className="voice-status">
            {isRecording
              ? (language === 'hi' ? '🔴 सुन रहा हूँ...' : '🔴 Listening...')
              : (language === 'hi' ? 'बोलने के लिए दबाएं' : 'Tap to speak')
            }
          </span>
          {step === 'conversation' && (
            <button className="btn btn--secondary btn--sm" onClick={() => setStep('documents')} style={{ marginLeft: 'auto' }}>
              {language === 'hi' ? 'दस्तावेज़ अपलोड करें' : 'Upload Documents →'}
            </button>
          )}
        </div>
      </div>
    );
  }

  // ─── Document Upload Step ────────────────────────────────────────────────

  if (step === 'documents') {
    return (
      <div className="intake-layout">
        <div className="intake-header">
          <div className="sidebar-logo">
            <div className="sidebar-logo-icon">M</div>
            <span className="sidebar-logo-text"><span>MediKiosk</span></span>
          </div>
          <span className="badge badge--primary badge--lg">
            {language === 'hi' ? 'दस्तावेज़ अपलोड' : 'Document Upload'}
          </span>
        </div>
        <div className="intake-body">
          <div style={{ maxWidth: '600px', width: '100%' }}>
            <h2 className="text-2xl font-bold" style={{ textAlign: 'center', marginBottom: '8px' }}>
              {language === 'hi' ? 'मेडिकल दस्तावेज़ अपलोड करें' : 'Upload Medical Documents'}
            </h2>
            <p className="text-secondary text-sm" style={{ textAlign: 'center', marginBottom: '32px' }}>
              {language === 'hi' ? 'पुराने प्रिस्क्रिप्शन, रिपोर्ट, या डिस्चार्ज समरी अपलोड करें' : 'Upload previous prescriptions, lab reports, or discharge summaries (optional)'}
            </p>

            <div className="upload-zone" onClick={() => {
              // Simulate document upload
              if (sessionId) {
                api.uploadDocument({ sessionId, fileName: 'prescription_sample.jpg', mimeType: 'image/jpeg', fileSize: 1024000 });
              }
            }}>
              <div className="upload-icon">📄</div>
              <div className="upload-text">{language === 'hi' ? 'दस्तावेज़ अपलोड करने के लिए टैप करें' : 'Tap to upload document'}</div>
              <div className="upload-hint">{language === 'hi' ? 'JPG, PNG, या PDF — 20MB तक' : 'JPG, PNG, or PDF — up to 20MB'}</div>
            </div>

            <div style={{ display: 'flex', gap: '12px', marginTop: '24px' }}>
              <button className="btn btn--secondary btn--lg" style={{ flex: 1 }} onClick={goToComplete}>
                {language === 'hi' ? 'छोड़ें' : 'Skip'}
              </button>
              <button className="btn btn--primary btn--lg" style={{ flex: 2 }} onClick={goToComplete}>
                {language === 'hi' ? 'पूरा हुआ' : 'Finish Intake ✓'}
              </button>
            </div>
          </div>
        </div>
      </div>
    );
  }

  // ─── Complete Step ───────────────────────────────────────────────────────

  return (
    <div className="intake-layout">
      <div className="intake-header">
        <div className="sidebar-logo">
          <div className="sidebar-logo-icon">M</div>
          <span className="sidebar-logo-text"><span>MediKiosk</span></span>
        </div>
      </div>
      <div className="intake-body">
        <div className="card" style={{ maxWidth: '500px', textAlign: 'center' }}>
          <div style={{ fontSize: '64px', marginBottom: '16px' }}>✅</div>
          <h2 className="text-2xl font-bold" style={{ marginBottom: '8px' }}>
            {language === 'hi' ? 'चेक-इन पूरा हुआ!' : 'Check-in Complete!'}
          </h2>
          <p className="text-secondary" style={{ marginBottom: '24px' }}>
            {language === 'hi'
              ? 'आपकी जानकारी डॉक्टर के पास भेज दी गई है। कृपया अपनी बारी का इंतज़ार करें।'
              : 'Your information has been sent to the doctor. A clinical summary is being prepared. Please wait for your turn.'
            }
          </p>
          <div className="alert alert--success" style={{ textAlign: 'left' }}>
            <span className="alert-icon">ℹ️</span>
            <div className="alert-content">
              <div className="alert-title">{language === 'hi' ? 'आगे क्या होगा?' : 'What happens next?'}</div>
              <p>{language === 'hi'
                ? 'डॉक्टर आपकी जानकारी की समीक्षा करेंगे और परामर्श के दौरान आपसे चर्चा करेंगे।'
                : 'The physician will review your information and discuss it with you during the consultation. The AI-generated summary is a draft until the doctor verifies it.'
              }</p>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
