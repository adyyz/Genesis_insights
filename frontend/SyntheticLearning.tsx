import React, { useEffect, useRef, useState } from 'react';
import { request } from './api';
import { Dialog } from './Dialog';
import { SeededHistory } from './SeededHistory';
import { useCorpusSummary } from './useCorpusSummary';

type Cutoffs = { tp: number; fp: number };
type Comparison = { cases: number; before: Record<string, number>; after: Record<string, number>; baseline_cutoffs: Cutoffs; applied_cutoffs: Cutoffs };
type Learning = { id: string; revision: number; state: string; comparison?: Comparison; body: {
  baseline_policy: { rules: Record<string, Cutoffs> }; results: { rule_id: string; sample_size: number; status: string; candidate: { thresholds: Cutoffs } | null }[];
}; validation: { results: { rule_id: string; passed: boolean }[] } | null };
type Exercise = { record: Learning | null; policy: { policy_id: string } };
type AppConfig = { mode: 'local' | 'cloud' };
const words = (text: string) => text.toLowerCase().replaceAll('_', ' ');
const protectedRules = new Set(['POST_DEATH', 'STRUCTURING']);

export function SyntheticLearning({ token, onError, navigate }: { token: string; onError: (e: unknown) => void; navigate: (action: () => void) => void }) {
  const [scope, setScope] = useState('exercise'), [mode, setMode] = useState<AppConfig['mode'] | null>(null), [history, setHistory] = useState<Learning[]>([]);
  const [record, setRecord] = useState<Learning | null>(null), [loading, setLoading] = useState(true), [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState(false), [version, setVersion] = useState(0), [confirmation, setConfirmation] = useState(''), [message, setMessage] = useState('');
  const summary = useCorpusSummary(onError, version);
  const owner = useRef({ token, scope }); owner.current = { token, scope };
  const lifetime = useRef(new AbortController()); useEffect(() => () => lifetime.current.abort(), []);
  const exercise = mode !== 'cloud' && scope === 'exercise';
  useEffect(() => {
    const abort = new AbortController();
    request<AppConfig>('/api/config', '', { signal: abort.signal }).then(config => {
      if (!abort.signal.aborted) { setMode(config.mode); if (config.mode === 'cloud') setScope('corpus'); }
    }).catch(e => { if (!abort.signal.aborted) onError(e); });
    return () => abort.abort();
  }, [onError]);
  useEffect(() => {
    if (!mode) return;
    const abort = new AbortController(); setLoading(true); setFailed(false); setRecord(null); setHistory([]); setConfirmation(''); setMessage('');
    const path = exercise ? '/api/operator/cutoff-exercise' : '/api/operator/bulk/learning?limit=50';
    request<Exercise | { items: Learning[] }>(path, token, { signal: abort.signal }).then(value => {
      if (abort.signal.aborted) return;
      if ('record' in value) setRecord(value.record);
      else { setHistory(value.items); setRecord(value.items[0] || null); }
    }).catch(e => { if (!abort.signal.aborted) { setFailed(true); onError(e); } }).finally(() => { if (!abort.signal.aborted) setLoading(false); });
    return () => abort.abort();
  }, [token, scope, version, mode]);
  const candidates = record?.body.results.filter(r => r.candidate) || [];
  const passed = record?.validation?.results.some(r => r.passed) || false;
  const unchanged = !!record && !candidates.length;
  const actions = [
    { id: 'validate', label: 'Validate candidate', icon: '🔎', enabled: !!record && record.state === 'PROPOSED' && !!candidates.length && !record.validation },
    { id: 'approve', label: 'Approve candidate', icon: '✓', enabled: record?.state === 'PROPOSED' && passed },
    { id: 'reject', label: 'Reject candidate', icon: '↩', enabled: record?.state === 'PROPOSED' && !!candidates.length },
    { id: 'apply', label: 'Apply cutoffs', icon: '⚖️', enabled: record?.state === 'APPROVED' && passed },
    ...(exercise ? [{ id: 'test', label: 'Test 100 cases', icon: '🧪', enabled: record?.state === 'APPLIED' && !record.comparison }] : []),
  ];
  async function act(action: string) {
    if (busy) return;
    const identity = { token, scope }; setBusy(true); setMessage('');
    try {
      const path = exercise ? '/api/operator/cutoff-exercise/' + action : '/api/operator/bulk/learning' + (action === 'prepare' ? '' : `/${record!.id}/${action}`);
      const result = await request<Exercise | Learning>(path, token, { method: 'POST', signal: lifetime.current.signal, body: JSON.stringify(action === 'prepare' && !exercise ? {} : { expected_revision: record?.revision || 0 }) });
      if (owner.current.token !== identity.token || owner.current.scope !== identity.scope) return;
      const next = 'record' in result ? result.record : result;
      setRecord(next); setHistory(old => next ? [next, ...old.filter(r => r.id !== next.id)] : old);
      setMessage(action === 'apply' ? 'Cutoffs applied to this isolated test policy only. Genuine investigation policy is unchanged.' : action === 'test' ? '100 controlled test cases compared. No model calls or human reviews were created.' : `${words(action)} completed. Review the recorded result before continuing.`);
    } catch (e) { if (owner.current.token === identity.token && owner.current.scope === identity.scope && !lifetime.current.signal.aborted) onError(e); }
    finally { if (owner.current.token === identity.token && owner.current.scope === identity.scope) setBusy(false); }
  }
  return <><SeededHistory token={token} onError={onError} /><section className="panel synthetic-learning" aria-label="Cutoff learning and controlled exercise">
    <div className="panel-heading"><h2><span aria-hidden="true">🧬 </span>Cutoff learning</h2><span className="badge neutral">Python calibration · zero model calls</span></div>
    <p>Risk weights and heuristic coefficients stay fixed. Only AUTO_TP / AUTO_FP boundaries are tested; approval and application are separate actions.</p>
    <div className="learning-controls"><div className="form-field"><label htmlFor="learning-dataset">Learning dataset</label><select id="learning-dataset" disabled={busy || !mode} value={scope} onChange={e => { const value = e.target.value; navigate(() => setScope(value)); }}>{mode !== 'cloud' && <option value="exercise">Controlled exercise / isolated fixture</option>}<option value="corpus">Existing corpus / {summary?.training_transactions.toLocaleString() ?? 'Loading'} training references</option></select></div><button className="secondary" disabled={busy || loading} onClick={() => setVersion(v => v + 1)}><span aria-hidden="true">↻ </span>Refresh learning</button></div>
    <div className="learning-scope"><strong>{exercise ? 'Controlled fixture—not a measured corpus improvement.' : 'Synthetic reference learning—not human feedback.'}</strong><p>{exercise ? '40 training, 20 independent validation and 100 later test cases. Deliberately separable scores exercise the real calibration and application code in separate local collections. No genuine policies, reviews or investigations change.' : <>Uses the active {summary?.training_transactions.toLocaleString() ?? 'Loading'} training references, including the {summary?.reference_walkthrough_cases.toLocaleString() ?? 'Loading'} displayed walkthrough cases. {summary?.scope === 'ACTIVE_BASELINE' ? 'Reserved cases remain unopened; independent validation is not available in this baseline.' : 'The separate validation cohort is opened once after candidate freeze. Historical results are retained.'}</>}</p></div>
    <ol className="learning-steps" aria-label="Cutoff exercise steps">{['Prepare', 'Validate', 'Approve', 'Apply', 'Compare 100'].slice(0, exercise ? 5 : 4).map((step, i) => <li key={step}><span aria-hidden="true">{i + 1}</span>{step}</li>)}</ol>
    {!mode ? <p role="status">Loading learning configuration...</p> : loading ? <p role="status">Loading learning records...</p> : failed ? <p role="status">Learning records unavailable. Use Refresh learning to retry.</p> : <>
      <div className="learning-actions"><button className="primary" disabled={busy || exercise && !!record} onClick={() => setConfirmation('prepare')}><span aria-hidden="true">🧬 </span>{exercise ? 'Prepare exercise candidate' : 'Prepare synthetic candidates'}</button></div>
      {!exercise && history.length > 0 && <div className="form-field learning-history"><label htmlFor="synthetic-history">Stored synthetic learning</label><select id="synthetic-history" disabled={busy} value={record?.id || ''} onChange={e => setRecord(history.find(r => r.id === e.target.value) || null)}>{history.map(r => <option key={r.id} value={r.id}>{r.body.results.some(item => item.candidate) ? words(r.state) : 'baseline retained'} / {r.id.slice(0, 8)}</option>)}</select></div>}
      {!record ? <p>Prepare the candidate to begin. No policy has changed.</p> : <><p className="learning-state"><strong>{unchanged ? 'Baseline retained' : `State: ${words(record.state)}`}</strong>{unchanged ? ' · No beneficial candidate was found under the safety checks.' : ' · Changes apply only to the selected test scope.'}</p>
        <div className="table-scroll"><table className="learning-table"><caption>{exercise ? 'Controlled HIGH_VALUE fixture' : 'All nine rules'} · counts are eligible scored observations; multi-rule cases may overlap.</caption><thead><tr><th>Rule</th><th>Eligible observations</th><th>Current → candidate cutoffs</th><th>Training finding</th><th>Independent validation</th></tr></thead><tbody>{record.body.results.filter(r => !exercise || r.sample_size > 0).map(row => {
          const baseline = record.body.baseline_policy.rules[row.rule_id];
          const validation = record.validation?.results.find(v => v.rule_id === row.rule_id);
          const guard = protectedRules.has(row.rule_id);
          return <tr key={row.rule_id}><th scope="row">{words(row.rule_id)}</th><td>{row.sample_size.toLocaleString()}</td><td><span className="cutoff-pair">TP ≤ {baseline.tp} / FP ≥ {baseline.fp}</span><small>{row.candidate ? `Candidate: TP ≤ ${row.candidate.thresholds.tp} / FP ≥ ${row.candidate.thresholds.fp}` : 'Unchanged'}</small></td><td><span className="badge neutral">{guard ? 'Protected override' : words(row.status)}</span></td><td>{guard ? 'Not applicable—protected' : !row.candidate ? 'Not evaluated—no candidate' : !record.validation ? 'Awaiting validation' : validation ? validation.passed ? 'Passed safety checks' : 'Failed safety checks' : 'Not evaluated'}</td></tr>;
        })}</tbody></table></div>
        {unchanged && <p className="context-warning">There is no candidate to validate, approve or apply. Keep the baseline; additional cases do not guarantee a safe improvement.</p>}
        <div className="learning-actions">{actions.map(action => <button key={action.id} className={action.id === 'apply' ? 'primary' : 'secondary'} disabled={busy || !action.enabled} onClick={() => setConfirmation(action.id)}><span aria-hidden="true">{action.icon} </span>{action.label}</button>)}</div>
        {record.comparison && <section className="exercise-comparison" aria-label="Controlled 100-case comparison"><h3><span aria-hidden="true">📊 </span>100-case fixture result</h3><p>FP cutoff: {record.comparison.baseline_cutoffs.fp} → {record.comparison.applied_cutoffs.fp}. Fixed scores and coefficients; separate test policy.</p><div className="table-scroll"><table><thead><tr><th>Machine label</th><th>Baseline</th><th>Applied cutoff</th></tr></thead><tbody>{Object.entries(record.comparison.before).map(([label, count]) => <tr key={label}><th scope="row">{words(label)}</th><td>{count}</td><td>{record.comparison!.after[label]}</td></tr>)}</tbody></table></div><p className="muted">Controlled fixture evidence only. These 100 cases are not genuine investigations and do not demonstrate a real-world improvement.</p></section>}
      </>}
    </>}
    {busy && <p role="status">Saving the learning action…</p>}{message && <p role="status">{message}</p>}
    {confirmation && <Dialog title={`${words(confirmation)} ${exercise ? 'controlled exercise' : 'synthetic candidate'}?`} description="This records an explicit action for the selected test scope. Genuine policies, model attempts and human reviews remain unchanged." onClose={() => !busy && setConfirmation('')}><div className="dialog-actions"><button className="secondary" onClick={() => setConfirmation('')}>Keep reviewing</button><button className="primary" disabled={busy} onClick={() => { const action = confirmation; setConfirmation(''); void act(action); }}>Confirm learning action</button></div></Dialog>}
  </section></>;
}
