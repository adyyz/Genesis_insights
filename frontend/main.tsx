import React, { useEffect, useState } from 'react';
import { createRoot } from 'react-dom/client';
import type { CaseRecord, RunDetail, PrivateAlert } from './types';
import './style.css';
import { Icon } from './Icon';
import { request } from './api';
import { OperatorReview } from './OperatorReview';
import { Dialog } from './Dialog';
import { CompletionToast, RunUsageDialog, TOAST_DISMISS_MS } from './Usage';
import { Admin } from './Admin';
import { SyntheticLearning } from './SyntheticLearning';
import { ReferenceOverview } from './ReferenceOverview';
import { CorpusEvolution } from './CorpusEvolution';
import { BulkWorkspace } from './BulkWorkspace';
import { PublicEvolution } from './RuleIntelligence';
import type { GoogleSignIn } from './google-auth';
import { useCorpusSummary } from './useCorpusSummary';



function App() {
  const [runVersion, setRunVersion] = useState(0);
  const [reviewVersion, setReviewVersion] = useState(0);
  const reviewedRevisions = React.useRef(new Map<string, number>());
  const [dirty, setDirty] = useState(false);
  const [corpusDirty, setCorpusDirty] = useState(false), [evolutionDiscard, setEvolutionDiscard] = useState(0);
  const hasDirty = dirty || corpusDirty;
  const [pendingNavigation, setPendingNavigation] = useState<(() => void) | null>(null);
  const [runId, setRunId] = useState('');
  const [completion, setCompletion] = useState<RunDetail | null>(null);
  useEffect(() => {
    if (!completion) return;
    const timeout = window.setTimeout(() => setCompletion(null), TOAST_DISMISS_MS);
    return () => window.clearTimeout(timeout);
  }, [completion?.id]);
  const announced = React.useRef(new Set<string>());
  const [session, setSession] = useState<{ is_admin: boolean; live_ai_eligible: boolean } | null>(null);
  function navigate(action: () => void) { if (hasDirty) setPendingNavigation(() => action); else action(); }
  useEffect(() => {
    if (!hasDirty) return;
    const guard = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ''; };
    window.addEventListener('beforeunload', guard);
    return () => window.removeEventListener('beforeunload', guard);
  }, [hasDirty]);
  const [screen, setScreen] = useState('Investigations');
  const [supportingCase, setSupportingCase] = useState<CaseRecord | null>(null);
  const [matrixRule, setMatrixRule] = useState('HIGH_VALUE');
  const [targetAlert, setTargetAlert] = useState('');
  const [error, setError] = useState('');
  const [token, setToken] = useState('');
  const [signIn, setSignIn] = useState(false);
  const [roles, setRoles] = useState<{ id: number; name: string; kind: string; output: string; used_by: string }[]>([]);
  const tokenRef = React.useRef(token);
  tokenRef.current = token;
  useEffect(() => {
    setSession(null); setSupportingCase(null); setRunId(''); setCompletion(null); announced.current.clear();
    if (!token) { setCorpusDirty(false); setTargetAlert(''); if (screen === 'Admin') setScreen('Investigations'); return; }
    const controller = new AbortController();
    request<{ is_admin: boolean; live_ai_eligible: boolean }>('/api/operator/session', token, { signal: controller.signal }).then(value => { if (!controller.signal.aborted) { setSession(value); setError(previous => previous.startsWith('Your operator session expired.') ? '' : previous); } }).catch(error => { if (!controller.signal.aborted) showError(error); });
    return () => controller.abort();
  }, [token]);
  function announceRun(run: RunDetail) {
    if (run.initiated_by_me && ['COMPLETE', 'FAILED', 'DISPATCH_FAILED'].includes(run.state) && !announced.current.has(run.id)) {
      announced.current.add(run.id); setCompletion(run); setRunVersion(value => value + 1);
    }
  }
  const screens = ['Investigations', 'Evolution lab', 'System design', ...(token ? ['Admin'] : [])];
  function updateAlert(alert: PrivateAlert['alert']) {
    if (alert.feedback_revision > (reviewedRevisions.current.get(alert.id) || 0)) {
      reviewedRevisions.current.set(alert.id, alert.feedback_revision); setReviewVersion(v => v + 1);
    }
  }
  function showError(error: unknown) {
    if (tokenRef.current !== token) return;
    if ((error as { status?: number }).status === 401) { setToken(''); setError('Your operator session expired. Sign in again to continue.'); }
    else setError(previous => previous.startsWith('Your operator session expired.') ? previous : error instanceof TypeError || (error as Error).name === 'TimeoutError' ? 'Unable to reach the service. Check your connection and retry.' : (error as Error).message);
  }
  const corpusSummary = useCorpusSummary(showError, runVersion + reviewVersion);

  useEffect(() => {
    const controller = new AbortController();
    request<{ roles: typeof roles }>('/api/system/agents', '', { signal: controller.signal }).then(data => setRoles(data.roles)).catch(e => { if (!controller.signal.aborted) showError(e); });
    return () => controller.abort();
  }, []);
  function openSupportingCase(caseId: string, alertId: string, ruleId: string) {
    navigate(() => { void request<CaseRecord>(`/api/operator/bulk/cases/${caseId}`, token).then(value => {
      if (tokenRef.current !== token) return;
      setMatrixRule(ruleId); setTargetAlert(alertId); setSupportingCase(value);
    }).catch(showError); });
  }

  return <div className="workspace">
    <aside className="sidebar"><button className="brand" onClick={() => navigate(() => setScreen('Investigations'))}><span className="brand-icon"><Icon name="eye" size={25} /></span><span>Genesis<small>INVESTIGATION SUITE</small></span></button>
      <div className="workspace-label"><span className="status-dot" /> Investigation workspace</div>
      <span className="nav-caption">WORKSPACE</span><nav aria-label="Main navigation" style={{ '--active-row': screens.indexOf(screen) } as React.CSSProperties}><span className="nav-selection" aria-hidden="true" />{screens.map((name, i) => <button aria-label={name} aria-current={screen === name ? 'page' : undefined} className={screen === name ? 'nav-link active' : 'nav-link'} onClick={() => navigate(() => setScreen(name))} key={name}><Icon name={(['investigations', 'evolution', 'system', 'shield'] as const)[i]} /><RollingLabel key={name + (screen === name ? '-selected' : '-idle')} text={name} />{i === 0 && <small aria-hidden="true" title={corpusSummary ? corpusSummary.cases.toLocaleString('en-AU') + ' cases in the active dataset' : 'Dataset count unavailable or loading'}>{corpusSummary ? corpusSummary.cases.toLocaleString('en-AU', { notation: 'compact', maximumFractionDigits: 1 }).toLowerCase() : '...'}</small>}</button>)}</nav>
      <div className="sidebar-note"><Icon name="shield" size={22} /><br /><span className="eyebrow">HUMAN IN THE LOOP</span><p>Intelligence informs.<br />Analysts decide.</p><small>Monitoring changes require explicit approval.</small></div>
      <button className="operator" onClick={() => navigate(() => token ? setToken('') : setSignIn(true))}><span className="avatar">{token ? 'OP' : 'PV'}</span><span>{token ? 'Operator' : 'Public visitor'}<small>{token ? 'Sign out' : 'Read-only · Sign in'}</small></span><Icon name="arrow" size={16} /></button>
    </aside>
    <main><header className="topbar"><span>Workspace <span className="slash">/</span> <strong>{screen}</strong></span></header>
      <div className="page-heading"><div><span className="eyebrow">FINANCIAL CRIME INTELLIGENCE</span><h1>{screen}</h1><p>{screen === 'Investigations' ? 'Review the signals. Follow the evidence. Make an informed decision.' : screen === 'Evolution lab' ? 'Track analyst outcomes by identity, typology and detector category.' : screen === 'Admin' ? 'Inspect measured usage and manage daily AI controls.' : 'Six focused roles. One accountable analyst workflow.'}</p></div></div>
      <div className="notice"><span><Icon name="info" size={17} /></span><span><strong>Synthetic demonstration.</strong> Source facts and risk scores are computed. Model suggestions require validation and human review. No real customer data.</span></div>
      {error && <div role="alert" className="error">{error}<button onClick={() => setError('')} aria-label="Dismiss error"><Icon name="close" size={16} /></button></div>}
      {screen === 'Investigations' && <BulkWorkspace token={token} navigate={navigate} onError={showError} onDirty={setDirty} onRun={setRunId} onComplete={announceRun} />}
      <>
        {screen === 'Admin' && token && <Admin token={token} isAdmin={!!session?.is_admin} canEnable={!!session?.live_ai_eligible} onError={showError} onRun={setRunId} />}
        {screen === 'Evolution lab' && <>{token ? <CorpusEvolution discardVersion={evolutionDiscard} token={token} selectedRule={matrixRule} onSelectRule={setMatrixRule} onOpenCase={openSupportingCase} canRun={!!session?.live_ai_eligible} onError={showError} onDirty={setCorpusDirty} navigate={navigate} /> : <><PublicEvolution /><section className="panel"><h2>Genuine cutoff learning</h2><p>Operator access is required to inspect human reviews, learn cutoffs or apply a policy.</p><button className="primary" onClick={() => setSignIn(true)}>Sign in to open cutoff learning</button></section></>}<details className="panel evo-practice"><summary><span aria-hidden="true">🧪 </span>Practice with synthetic data</summary><p>These references and exercises are separate from genuine investigations and operational policy.</p><ReferenceOverview onError={showError} />{token && <SyntheticLearning token={token} onError={showError} navigate={navigate} />}</details></>}
        {screen === 'System design' && <><section className="panel"><div className="panel-heading"><h2><span aria-hidden="true">🤖 </span>Six roles. Explicit handovers.</h2><span className="badge neutral">4 Gemini roles · 2 deterministic</span></div><div className="flow">{roles.map(role => <React.Fragment key={role.id}><span>A{role.id}<small>{role.name}</small></span></React.Fragment>)}</div><p className="muted">A5 analyzes a frozen human-feedback snapshot separately from triage. Small samples remain insufficient evidence; all proposals require review.</p><div className="table-scroll"><table><thead><tr><th>Role</th><th>Implementation</th><th>Output</th><th>Used by</th></tr></thead><tbody>{roles.map(role => <tr key={role.id}><td><strong>A{role.id} · {role.name}</strong></td><td>{role.kind}</td><td>{role.output}</td><td>{role.used_by}</td></tr>)}</tbody></table></div></section><div className="detail-grid"><section className="panel"><h3>Firestore is the source of truth</h3><p>Source records → alerts → triage runs → feedback events → evolution runs → recommendations → evaluations. Stable IDs link each step. Append-only application audit events record actors and versions.</p><p className="muted">Browser access to Firestore is denied. All reads and writes go through the API.</p></section><section className="panel"><h3>Published evidence, bounded operations</h3><p>Public visitors read a frozen, synthetic publication. Authenticated operators use server-verified Firebase tokens. Live calls require verified access, daily controls and a separate worker. Public browsing never starts a model request.</p><p className="muted">Local emulators require no cloud billing.</p></section></div></>}
      </>{supportingCase && token && <Dialog wide title="Supporting investigation" description="Inspect the linked evidence. Close to return to the selected Evolution rule." onClose={() => navigate(() => setSupportingCase(null))}><h3>{supportingCase.name}</h3><OperatorReview initialAlert={targetAlert} key={supportingCase.id} current={supportingCase} token={token} version={runVersion} onError={showError} onDirty={setDirty} navigate={navigate} onRun={setRunId} onComplete={announceRun} onStatus={updateAlert} /></Dialog>}<footer>Genesis <span>Evidence-led investigation</span><span>Synthetic data · Human decisions</span></footer>
    </main>
    {pendingNavigation && <Dialog title="Discard unsaved review?" description="Your review has unsaved changes. Keep editing or discard them before leaving." onClose={() => setPendingNavigation(null)}><div className="dialog-actions"><button className="primary" onClick={() => setPendingNavigation(null)}>Keep editing</button><button className="text-button" onClick={() => { const action = pendingNavigation; setPendingNavigation(null); setDirty(false); setCorpusDirty(false); setEvolutionDiscard(value => value + 1); action(); }}>Discard changes</button></div></Dialog>}
    {runId && token && <RunUsageDialog runId={runId} token={token} onClose={() => setRunId('')} onError={showError} />}
    {completion && token && <CompletionToast key={completion.id} run={completion} onOpen={() => setRunId(completion.id)} onDismiss={() => setCompletion(null)} />}
    {signIn && <SignIn onClose={() => setSignIn(false)} onToken={value => { setCorpusDirty(false); setToken(value); setSignIn(false); setError(''); }} />}
  </div>;
}

function RollingLabel({ text }: { text: string }) {
  return <span className="rolling-label" aria-hidden="true">{Array.from(text).map((letter, index) => <span className="rolling-letter" key={index} style={{ '--letter-index': index } as React.CSSProperties}><span className="rolling-letter-track"><span>{letter === ' ' ? '\u00a0' : letter}</span><span className="rolling-letter-copy">{letter === ' ' ? '\u00a0' : letter}</span></span></span>)}</span>;
}

function SignIn({ onClose, onToken }: { onClose: () => void; onToken: (token: string) => void }) {
  type AuthConfig = { mode: 'local' | 'cloud'; project_id: string; auth_emulator_url: string | null; firebase_web_api_key: string | null; operator_demo_email?: string };
  const [config, setConfig] = useState<AuthConfig | null>(null);
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(true);
  const [visible, setVisible] = useState(false);
  const [invalid, setInvalid] = useState({ email: '', password: '' });
  const emailRef = React.useRef<HTMLInputElement>(null);
  const passwordRef = React.useRef<HTMLInputElement>(null);
  const controller = React.useRef(new AbortController());
  const google = React.useRef<GoogleSignIn | null>(null);
  const googleError = React.useRef<(error: unknown) => string>(() => 'Google sign-in failed. Try again.');
  const [googleReady, setGoogleReady] = useState(false);
  async function loadConfiguration() {
    setBusy(true); setError(''); setGoogleReady(false);
    try {
      const value = await request<AuthConfig>('/api/config', '', { signal: controller.current.signal });
      if (controller.current.signal.aborted) return;
      setConfig(value);
      if (value.mode === 'local' && value.auth_emulator_url) {
        setEmail('operator@aml-insight.test'); setPassword('local-demo-only');
      } else if (value.mode === 'cloud' && value.firebase_web_api_key && value.project_id) {
        setEmail(value.operator_demo_email || ''); setPassword('');
        try {
          const { prepareGoogleSignIn, googleSignInError } = await import('./google-auth');
          if (controller.current.signal.aborted) return;
          google.current = prepareGoogleSignIn(value.project_id, value.firebase_web_api_key);
          googleError.current = googleSignInError;
          setGoogleReady(true);
        } catch { if (!controller.current.signal.aborted) setError('Google sign-in could not be prepared. Use email and password, or retry setup.'); }
      } else {
        setError('Operator sign-in is not configured. Contact the administrator.');
      }
    } catch { if (!controller.current.signal.aborted) setError('Unable to load sign-in. Check your connection and retry.'); }
    finally { if (!controller.current.signal.aborted) setBusy(false); }
  }
  useEffect(() => { void loadConfiguration(); return () => { controller.current.abort(); void google.current?.dispose(); }; }, []);
  const ready = !!config && (config.mode === 'local' ? !!config.auth_emulator_url : !!config.firebase_web_api_key);
  async function signInGoogle() {
    if (busy || !googleReady || !google.current) return;
    setBusy(true); setError('');
    const signal = controller.current.signal;
    try {
      const idToken = await google.current.signIn(signal);
      if (signal.aborted) return;
      await request('/api/operator/session', idToken, { signal });
      if (!signal.aborted) onToken(idToken);
    } catch (failure) {
      if (!signal.aborted) setError(googleError.current(failure));
    } finally { if (!signal.aborted) setBusy(false); }
  }
  async function submit(event: React.FormEvent) {
    event.preventDefault(); if (busy || !ready || !config) return;
    const fields = { email: !email.trim() || emailRef.current?.validity.typeMismatch ? 'Enter a valid email address.' : '', password: !password ? 'Enter your password.' : '' };
    setInvalid(fields); setError('');
    if (fields.email || fields.password) { (fields.email ? emailRef : passwordRef).current?.focus(); return; }
    setBusy(true);
    const signal = AbortSignal.any([controller.current.signal, AbortSignal.timeout(15000)]);
    try {
      const endpoint = config.mode === 'local'
        ? `${config.auth_emulator_url}/identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key=emulator-only`
        : `https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key=${encodeURIComponent(config.firebase_web_api_key!)}`;
      const response = await fetch(endpoint, { method: 'POST', signal, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ email: email.trim(), password, returnSecureToken: true }) });
      const body = await response.json();
      if (!response.ok || typeof body.idToken !== 'string' || !body.idToken) throw new Error(config.mode === 'local' ? 'Local sign-in failed. Seed the emulator and check your details.' : 'Sign-in failed. Check your operator email and password.');
      await request('/api/operator/session', body.idToken, { signal });
      if (!signal.aborted) { setPassword(''); onToken(body.idToken); }
    } catch (e) { if (!controller.current.signal.aborted) setError(e instanceof TypeError || (e as Error).name === 'TimeoutError' ? 'Unable to reach sign-in. Check your connection and retry.' : (e as Error).message); } finally { if (!controller.current.signal.aborted) setBusy(false); }
  }
  async function storedLogin() {
    if (busy || config?.mode !== 'cloud' || !config.operator_demo_email) return;
    setBusy(true); setError('');
    const signal = controller.current.signal;
    try {
      const result = await request<{idToken: string}>('/api/operator/demo-login', '', { method: 'POST', signal });
      if (typeof result.idToken !== 'string' || !result.idToken) throw new Error('Stored operator sign-in failed.');
      if (signal.aborted) return;
      await request('/api/operator/session', result.idToken, { signal });
      if (!signal.aborted) { setPassword(''); onToken(result.idToken); }
    } catch (failure) { if (!signal.aborted) setError((failure as Error).message); }
    finally { if (!signal.aborted) setBusy(false); }
  }
  return <Dialog title="Operator sign-in" description={config?.mode === 'local' ? 'These fixture credentials work only with the local emulator. No cloud account is used.' : 'Operator email and password. Google sign-in stays available.'} onClose={onClose}>
    {config?.mode === 'cloud' && <div className="google-signin-flow">
      <button className="google-signin" type="button" disabled={busy || !googleReady} aria-busy={busy} onClick={() => void signInGoogle()}><Icon name="google" size={20} /><span>{busy && googleReady ? 'Signing in…' : 'Continue with Google'}</span></button>
      <p>Your operator session stays in this tab and ends when you reload or sign out.</p>
      {config.operator_demo_email && <button className="secondary" type="button" disabled={busy} onClick={() => void storedLogin()}>Use stored operator login</button>}
    </div>}
    <form noValidate onSubmit={submit}>
      <div className="form-field"><label htmlFor="operator-email">Email</label><input id="operator-email" ref={emailRef} type="email" autoComplete="username" required disabled={busy || !ready} value={email} aria-invalid={!!invalid.email} aria-describedby={invalid.email ? 'email-error' : undefined} onChange={e => setEmail(e.target.value)} />{invalid.email && <span className="field-error" id="email-error">{invalid.email}</span>}</div>
      <div className="form-field"><label htmlFor="operator-password">Password</label><span className="password-control"><input id="operator-password" ref={passwordRef} type={visible ? 'text' : 'password'} autoComplete="current-password" required disabled={busy || !ready} value={password} aria-invalid={!!invalid.password} aria-describedby={invalid.password ? 'password-error' : undefined} onChange={e => setPassword(e.target.value)} /><button type="button" onClick={() => setVisible(!visible)} aria-label={visible ? 'Hide password' : 'Show password'} aria-pressed={visible}>{visible ? 'Hide' : 'Show'}</button></span>{invalid.password && <span className="field-error" id="password-error">{invalid.password}</span>}</div>
      {error && <p className="error" role="alert">{error}</p>}{(!ready || (config?.mode === 'cloud' && !googleReady)) && !busy && <button className="text-button" type="button" onClick={() => void loadConfiguration()}>Retry sign-in setup</button>}<button className="primary" disabled={busy || !ready} aria-busy={busy}>{busy ? 'Verifying…' : config?.mode === 'local' ? 'Sign in locally' : 'Sign in with email'}</button>
    </form>
  </Dialog>;
}

createRoot(document.getElementById('root')!).render(<App />);
