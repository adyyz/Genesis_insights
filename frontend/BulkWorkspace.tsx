import React, { useEffect, useRef, useState } from 'react';
import { request } from './api';
import { Dialog } from './Dialog';
import { Icon } from './Icon';
import { CaseStages } from './CaseStages';
import { syntheticLabel } from './bulk-label';
import { UsageDisplay } from './Usage';
import { OperatorReview } from './OperatorReview';
import { ReferenceOverview } from './ReferenceOverview';
import type { CorpusSummary } from './useCorpusSummary';
import type { CaseRecord, PrivateAlert, RunDetail, UsageSummary } from './types';

type Row = { id: string; identity_id: string; name: string; customer_type: string; rules: string[]; heuristic_score: number; requires_review: boolean; risk_tier: string; batch_state?: string | null; batch_id?: string; triaged?: boolean; reviewed?: boolean; reference?: boolean; reference_verdict?: string; reference_prediction?: string; reference_score?: number };
type BulkCase = CaseRecord & { reference_record?: Row | null };
type Page = { items: Row[]; total: number; next_cursor: number | null };
type Summary = CorpusSummary & { eligible_pending_cases: number; rule_alerts: Record<string, number>; training_detector_metrics: { TP: number; FP: number; TN: number; FN: number; inconclusive: number } | null; policy: { policy_id: string; rules: Record<string, { tp: number; fp: number }> } };
type Preview = { case_ids?: string[]; selection_hash: string; dataset_id: string; eligible_cases: number; alerts: number; minimum_generation_attempts: number; maximum_generation_attempts: number; authorization_available: boolean };
type GenuineSummary = { triaged_cases: number; reviewed_cases: number; triaged_alerts: number; reviewed_alerts: number; true_positive: number; false_positive: number; inconclusive: number };
type Job = { id: string; state: string; revision: number; waiting_message?: string | null; can_resume?: boolean; counts: Record<string, number>; members: { case_id: string; state: string; parent_id: string | null; error_code?: string }[] };
const names = (s: string) => s.toLowerCase().replaceAll('_', ' ');

export function BulkWorkspace({ token, navigate, onError, onDirty, onRun, onComplete }: { token: string; navigate: (fn: () => void) => void; onError: (e: unknown) => void; onDirty: (value: boolean) => void; onRun: (id: string) => void; onComplete: (run: RunDetail) => void }) {
  const [batchHistory, setBatchHistory] = useState<{ id: string; state: string }[]>([]);
  const [accounting, setAccounting] = useState<UsageSummary | null>(null);
  const [summary, setSummary] = useState<Summary | null>(null), [page, setPage] = useState<Page | null>(null);
  const [identities, setIdentities] = useState<{ identity_id: string; name: string; customer_type: string }[]>([]);
  const [reviewCase, setReviewCase] = useState<CaseRecord | null>(null);
  const [referenceCase, setReferenceCase] = useState<Row | null>(null);
  const [detailsOpen, setDetailsOpen] = useState(false), [detailsFailed, setDetailsFailed] = useState(false);
  const [selected, setSelected] = useState(''), [current, setCurrent] = useState<BulkCase | null>(null);
  const [query, setQuery] = useState(''), [type, setType] = useState(''), [identity, setIdentity] = useState(''), [rule, setRule] = useState('');
  const [triageStatus, setTriageStatus] = useState('all');
  const [cursor, setCursor] = useState(0), [previous, setPrevious] = useState<number[]>([]);
  const [size, setSize] = useState(10), [busy, setBusy] = useState(''), [message, setMessage] = useState('');
  const [preview, setPreview] = useState<{ rows: Row[]; live: Preview | null; single: boolean; key: string } | null>(null);
  const [job, setJob] = useState<Job | null>(null);
  const [confirmation, setConfirmation] = useState<{ title: string; action: () => Promise<void> } | null>(null);
  const [predictions, setPredictions] = useState<Record<string, Record<string, string>>>({});
  const [version, setVersion] = useState(0), [genuine, setGenuine] = useState<GenuineSummary | null>(null);
  const reviewedRevisions = useRef(new Map<string, number>());
  const live = useRef(true); useEffect(() => { live.current = true; return () => { live.current = false; }; }, []);
  const session = useRef(token); session.current = token;
  useEffect(() => { setPreview(null); setJob(null); setCurrent(null); setBusy(''); setAccounting(null); setGenuine(null); setBatchHistory([]); setReviewCase(null); setReferenceCase(null); setDetailsOpen(false); }, [token]);
  useEffect(() => {
    const abort = new AbortController();
    request<{ items: typeof identities }>('/api/demo/bulk/identities', '', { signal: abort.signal }).then(value => setIdentities(value.items)).catch(e => { if (!abort.signal.aborted) onError(e); });
    request<Summary>('/api/demo/bulk/summary', '', { signal: abort.signal }).then(setSummary).catch(e => { if (!abort.signal.aborted) onError(e); });
    if (token) {
      request<{items: typeof batchHistory}>('/api/operator/triage-batches?limit=50', token, { signal: abort.signal }).then(value => { if (!abort.signal.aborted) setBatchHistory(value.items); }).catch(e => { if (!abort.signal.aborted) onError(e); });
    }
    if (token) request<UsageSummary>('/api/operator/bulk/usage', token, { signal: abort.signal }).then(setAccounting).catch(e => { if (!abort.signal.aborted) onError(e); });
    if (token) request<GenuineSummary>('/api/operator/bulk/summary', token, { signal: abort.signal }).then(setGenuine).catch(e => { if (!abort.signal.aborted) onError(e); });
    return () => abort.abort();
  }, [token, version]);
  function parameters(limit = 50, at = cursor) { return new URLSearchParams({ limit: String(limit), cursor: String(at), q: query, customer_type: type, identity, rule, status: triageStatus }).toString(); }
  useEffect(() => {
    const abort = new AbortController(); setPage(null);
    request<Page>((token ? '/api/operator/bulk/cases?' : '/api/demo/bulk/cases?') + parameters(), token, { signal: abort.signal }).then(value => {
      if (abort.signal.aborted) return; setPage(value); setSelected(old => value.items.some(r => r.id === old) ? old : value.items[0]?.id || '');
    }).catch(e => { if (!abort.signal.aborted) onError(e); });
    return () => abort.abort();
  }, [query, type, identity, rule, cursor, token, triageStatus, version]);
  useEffect(() => {
    const abort = new AbortController(); setCurrent(null); setDetailsFailed(false);
    if (selected) request<BulkCase>(`/api/${token ? 'operator' : 'demo'}/bulk/cases/${selected}`, token, { signal: abort.signal }).then(value => { if (!abort.signal.aborted) setCurrent(value); }).catch(e => { if (!abort.signal.aborted) { setDetailsFailed(true); onError(e); } });
    return () => abort.abort();
  }, [selected, token, version]);
  function filter(fn: () => void) { navigate(() => { fn(); setDetailsOpen(false); setCursor(0); setPrevious([]); setPreview(null); }); }
  async function perform(name: string, action: () => Promise<void>) {
    if (busy) return; const owner = token; setBusy(name); setMessage('');
    try { await action(); } catch (e) { if (live.current && session.current === owner) onError(e); }
    finally { if (live.current && session.current === owner) setBusy(''); }
  }
  async function inspect(single = false, target?: Row) {
    await perform('preview', async () => {
      const rows: Row[] = []; let next = 0;
      if (single) { const found = target || page?.items.find(r => r.id === selected); if (found && !found.reference && !found.triaged && !found.batch_state) rows.push(found); }
      else do {
        const result = await request<Page>((token ? '/api/operator/bulk/cases?' : '/api/demo/bulk/cases?') + parameters(100, next).replace(/status=[^&]*/, 'status=awaiting_triage'), token);
        rows.push(...result.items.filter(row => !row.reference && !row.batch_state && row.rules.length > 0 && (token || !predictions[row.id])).slice(0, size - rows.length));
        if (result.next_cursor === null || rows.length >= size) break;
        next = result.next_cursor;
      } while (live.current);
      if (!rows.length) { setMessage('No eligible cases remain in this filter.'); return; }
      const result = token ? await request<Preview>('/api/operator/triage-batches/preview', token, { method: 'POST', body: JSON.stringify({ case_ids: rows.map(r => r.id), ...(summary?.dataset_id ? {dataset_id: summary.dataset_id} : {}) }) }) : null;
      if (result && !result.eligible_cases) { setMessage('No new eligible alerts remain in these cases. Choose Not fully triaged or another filter.'); return; }
      if (live.current && session.current === token) setPreview({ rows, live: result, single, key: crypto.randomUUID() });
    });
  }
  async function start() {
    if (!preview || !summary) return;
    const selectedPreview = preview;
    await perform('run', async () => {
      if (token && selectedPreview.live) {
        const result = await request<Job>('/api/operator/triage-batches', token, { method: 'POST', body: JSON.stringify({
          case_ids: selectedPreview.rows.map(r => r.id), dataset_id: selectedPreview.live.dataset_id,
          selection_hash: selectedPreview.live.selection_hash, idempotency_key: selectedPreview.key }) });
        if (live.current && session.current === token) { setPreview(null); setJob(result); setVersion(value => value + 1); setMessage(result.waiting_message || 'Batch saved. Refresh progress to see the worker outcome.'); }
      } else {
        for (const row of selectedPreview.rows) {
          if (!live.current || session.current !== token) break;
          const output = Object.fromEntries(row.rules.map(r => [r, syntheticLabel(row.heuristic_score, row.requires_review, summary.policy.rules[r].tp, summary.policy.rules[r].fp)]));
          setPredictions(previous => ({ ...previous, [row.id]: output }));
          await new Promise<void>(resolve => window.setTimeout(resolve, 0));
        }
        if (live.current && session.current === token) { setPreview(null); setMessage('Synthetic batch triaged. Predictions await review; no Gemini calls or human verdicts were created.'); }
      }
    });
  }
  async function showBatch(id: string) {
    await perform('batch', async () => {
      const result = await request<Job>('/api/operator/triage-batches/' + id, token);
      if (live.current && session.current === token) setJob(result);
    });
  }
  async function refresh() {
    await perform('refresh', async () => {
      const result = job ? await request<Job>('/api/operator/triage-batches/' + job.id, token) : null;
      if (live.current && session.current === token) { if (result) setJob(result); setVersion(v => v + 1); }
    });
  }
  function status(alert: PrivateAlert['alert']) {
    if (alert.feedback_revision > (reviewedRevisions.current.get(alert.id) || 0)) {
      reviewedRevisions.current.set(alert.id, alert.feedback_revision);
      request<GenuineSummary>('/api/operator/bulk/summary', token).then(value => {
        if (live.current && session.current === token) setGenuine(value);
      }).catch(e => { if (live.current && session.current === token) onError(e); });
    }
    setPage(old => old && ({ ...old, items: old.items.map(row => row.id === reviewCase?.id && alert.feedback_revision > 0 ? { ...row, reviewed: reviewCase.alerts.every(a => a.id === alert.id || a.frozen) } : row) }));
    setCurrent(old => old && ({ ...old, alerts: old.alerts.map(a => a.id === alert.id ? { ...a, status: alert.status, frozen: alert.feedback_revision > 0, actionable: alert.feedback_revision === 0 && !['COMPLETE', 'RUNNING', 'QUEUED'].includes(alert.status) } : a) }));
  }
  async function openReview(row: Row) {
    navigate(() => { void perform('review', async () => {
      const value = await request<CaseRecord>('/api/operator/bulk/cases/' + row.id, token);
      if (live.current && session.current === token) { setSelected(row.id); setReviewCase(value); }
    }); });
  }
  const simulated = Object.keys(predictions).length;
  const totalAlerts = summary ? Object.values(summary.rule_alerts).reduce((a, b) => a + b, 0) : 0;
  const activeBaseline = summary?.scope === 'ACTIVE_BASELINE';
  const advisory = summary?.advisory;
  const detectorMetrics = summary?.training_detector_metrics;
  return <>
    <div className="metrics bulk-metrics">
      {[
        ['Dataset', summary?.transactions.toLocaleString() ?? 'Loading', summary ? `${summary.identities.toLocaleString()} identities / ${summary.cases.toLocaleString()} candidate cases; ${summary.reference_walkthrough_cases.toLocaleString()} pre-evaluated references` : 'Loading corpus counts'],
        [activeBaseline ? 'Advisory coverage' : 'Triage progress', activeBaseline ? advisory ? (advisory.baseline_cases - advisory.pending_ai_reviews).toLocaleString() : 'Loading' : token ? (genuine ? String(genuine.triaged_cases) : 'Loading') : String(simulated), activeBaseline ? advisory ? `${advisory.ai_reviewed_cases} Gemini / ${advisory.agent_draft_reviewed_cases.toLocaleString()} agent drafts; ${advisory.pending_ai_reviews} awaiting advisory; ${advisory.reserved_cases.toLocaleString()} reserved. Not human reviews or full triage.` : 'Loading advisory coverage' : summary ? token ? genuine ? `Genuine triaged cases / ${summary.eligible_pending_cases} eligible; ${genuine.triaged_alerts} triaged alerts` : 'Genuine counts are loading; reference simulations are excluded' : `Simulated cases / ${summary.eligible_pending_cases} eligible; ${totalAlerts.toLocaleString()} source alerts across all partitions; ${summary.reference_walkthrough_cases.toLocaleString()} reference cases evaluated separately` : 'Loading corpus counts'],
        [token ? 'Analyst FP share' : activeBaseline ? 'Reference coverage' : 'Training detector FPR', token ? !genuine ? 'Loading' : (genuine.true_positive + genuine.false_positive ? `${(100 * genuine.false_positive / (genuine.true_positive + genuine.false_positive)).toFixed(1)}%` : 'Not available') : activeBaseline ? `${(100 * summary.reference_annotations / summary.cases).toFixed(1)}%` : detectorMetrics && detectorMetrics.FP + detectorMetrics.TN > 0 ? `${(100 * detectorMetrics.FP / (detectorMetrics.FP + detectorMetrics.TN)).toFixed(1)}%` : 'Not available', token ? genuine ? `FP / (TP + FP), ${genuine.true_positive + genuine.false_positive} conclusive human reviews; ${genuine.inconclusive} inconclusive` : 'Genuine review counts have not loaded' : activeBaseline ? 'Reference cases / active cases. The old corpus detector FPR does not describe this baseline.' : 'Synthetic training only: FP / (FP + TN). Validation is sealed; this is not A0 label accuracy.'],
        [token ? 'Analyst review' : 'Reference annotations', token ? (genuine ? String(genuine.reviewed_alerts) : 'Loading') : summary?.reference_annotations.toLocaleString() ?? 'Loading', token ? 'Frozen human-reviewed alerts; simulation labels are excluded' : summary ? `${summary.training_transactions.toLocaleString()} reference baseline / ${summary.validation_transactions.toLocaleString()} ${activeBaseline ? 'reserved cases' : 'sealed validation'}; not human reviews` : 'Loading reference counts'],
      ].map(([title, value, detail]) => <section className="metric" key={title}><span className="eyebrow">{title}</span><strong>{value}</strong><small>{detail}</small></section>)}
    </div>
    <div className="notice"><Icon name="info" size={17} /><span><strong>{activeBaseline ? 'Active Firestore baseline' : token ? 'Operator genuine execution' : 'Synthetic simulation / 0 model calls'}.</strong> {activeBaseline ? 'Reference labels, AI advisories and human verdicts are recorded separately. Reserved reference labels remain private.' : token ? 'A batch contains up to 100 cases. Access, expiry and daily controls still apply.' : 'Progress belongs to this tab and resets on reload. Sealed validation references remain private.'}</span></div>
    <section className="panel bulk-queue"><div className="panel-heading"><h2><span aria-hidden="true">📋 </span>Bulk investigation queue</h2><span>{page?.total.toLocaleString() ?? 'Loading'} matching cases</span></div>
    <div className="batch-toolbar"><span className="eyebrow">INVESTIGATE / BATCH TRIAGE</span><h3><span aria-hidden="true">⚡ </span>Start a batch</h3>
      <div className="bulk-actions"><button className="secondary" aria-pressed={size === 10} onClick={() => setSize(10)}>10 cases</button><button className="secondary" aria-pressed={size === 100} onClick={() => setSize(100)}>100 cases</button><label>Cases per batch<input aria-label="Cases per batch" type="number" min={1} max={100} value={size} onChange={e => setSize(Number(e.target.value))} /></label><button className="primary" disabled={!!busy || (!!token && summary?.live_triage_available === false) || size < 1 || size > 100 || !Number.isInteger(size)} onClick={() => void inspect()}>Preview {token ? 'Gemini' : 'synthetic'} batch</button><button className="secondary" disabled={!!busy} onClick={() => void refresh()}>Refresh queue</button>{token && <button className="secondary" disabled={!!busy || summary?.live_triage_available === false || !!current?.reference_record || !current?.alerts.some(a => a.actionable !== false)} onClick={() => void inspect(true)}>Check live triage</button>}</div>
      <p className="muted">Cases are triaged before analyst review. Reference annotations and synthetic predictions never complete a human review.</p>
      {token && summary?.live_triage_available === false && <p className="context-warning">Genuine execution is unavailable. Verified model access and the separate worker must be configured; cloud execution also requires authorized task dispatch.</p>}
      {message && <p role="status">{message}</p>}{busy && <p role="status">Working: {busy}...</p>}
      {token && batchHistory.length > 0 && <div className="form-field"><label htmlFor="bulk-job-history">Stored case batches</label><select id="bulk-job-history" value={job?.id || ''} onChange={e => { const id = e.target.value; if (id) void showBatch(id); }}><option value="">Select a stored batch</option>{batchHistory.map(r => <option key={r.id} value={r.id}>{names(r.state)} / {r.id.slice(0, 8)}</option>)}</select></div>}
      {job && <section className="batch-progress"><h3>Batch: {names(job.counts.RUNNING ? 'RUNNING' : job.state)}</h3><p>{Object.entries(job.counts).map(([name, value]) => `${name === 'PENDING' ? 'Not started' : names(name)}: ${value}`).join(' / ')}</p>{job.waiting_message && <p className="context-warning" role="status">{job.waiting_message}</p>}<div className="bulk-actions"><button className="secondary" disabled={!!busy} onClick={() => void refresh()}>Refresh batch progress</button>{['pause', 'resume', 'cancel'].map(action => <button className="secondary" key={action} disabled={!!busy || ['COMPLETE', 'FAILED', 'CANCELLED'].includes(job.state) || action === 'resume' && job.can_resume === false} onClick={() => setConfirmation({ title: action === 'resume' && job.state === 'PAUSED_OUTCOME' ? 'Acknowledge failures and continue remaining cases?' : `${names(action)} this batch?`, action: async () => { await perform(action, async () => { const result = await request<Job>(`/api/operator/triage-batches/${job.id}/${action}`, token, { method: 'POST', body: JSON.stringify({ expected_revision: job.revision, acknowledge_failed: action === 'resume' && job.state === 'PAUSED_OUTCOME' }) }); if (live.current && session.current === token) { setJob(result); setVersion(value => value + 1); } }); } })}>{names(action)}</button>)}</div><details><summary>Case outcomes</summary>{job.members.map(m => <p key={m.case_id}>{m.case_id.slice(0, 8)}: {m.state === 'PENDING' ? 'Not started' : names(m.state)} {m.error_code || ''} {m.parent_id && <button className="secondary" onClick={() => onRun(m.parent_id!)}>Inspect run usage</button>}</p>)}</details></section>}
    </div>
      {token && <button className="secondary" onClick={() => filter(() => { setTriageStatus('awaiting_review'); setQuery(''); setType(''); setIdentity(''); setRule(''); })}><span aria-hidden="true">📝 </span>Show cases awaiting analyst review</button>}
      <div className="bulk-filters">
        <label>Search identities<input value={query} onChange={e => { const value = e.target.value; filter(() => setQuery(value)); }} /></label>
        <label>Identity type<select aria-label="Identity type" value={type} onChange={e => { const value = e.target.value; filter(() => { setType(value); setIdentity(''); }); }}><option value="">All identity types</option><option value="INDIVIDUAL">Individuals</option><option value="ORGANIZATION">Organizations</option></select></label>
        <label>Identity<select aria-label="Identity" value={identity} onChange={e => { const value = e.target.value; filter(() => setIdentity(value)); }}><option value="">All identities</option>{identities.filter(r => !type || r.customer_type === type).map(r => <option key={r.identity_id} value={r.identity_id}>{r.name}</option>)}</select></label>
        <label>Detector rule<select aria-label="Detector rule" value={rule} onChange={e => { const value = e.target.value; filter(() => setRule(value)); }}><option value="">All nine rules</option>{Object.keys(summary?.rule_alerts || {}).map(r => <option key={r} value={r}>{names(r)}</option>)}</select></label>
        {token && <label>Triage status<select aria-label="Triage status" value={triageStatus} onChange={e => { const value = e.target.value; filter(() => setTriageStatus(value)); }}><option value="all">All available cases</option><option value="reference">Preloaded synthetic reference</option><option value="awaiting_triage">Not fully triaged</option><option value="awaiting_review">Triaged, awaiting review</option><option value="frozen">Frozen human reviews</option></select></label>}
        {!token && <label>Case status<select aria-label="Case status" value={triageStatus} onChange={e => { const value = e.target.value; filter(() => setTriageStatus(value)); }}><option value="all">All available cases</option><option value="reference">Preloaded synthetic reference</option><option value="awaiting_triage">Ready for simulation</option></select></label>}
      </div>
      {!page ? <p role="status">Loading case summaries...</p> : !page.items.length ? <p>No matching cases. <button className="secondary" onClick={() => filter(() => { setQuery(''); setType(''); setIdentity(''); setRule(''); setTriageStatus('all'); })}>Clear filters</button></p> : <div className="table-scroll bulk-table"><table><thead><tr><th>Identity / case</th><th>Rules</th><th>Source risk</th><th>{token ? 'Investigation state' : 'Simulation state'}</th><th>View details</th></tr></thead><tbody>{page.items.map(row => <tr key={row.id} className={selected === row.id ? 'selected-row' : ''}><td><button aria-pressed={selected === row.id} className="secondary" onClick={() => navigate(() => setSelected(row.id))}>{row.name} / {row.id.slice(0, 6)}</button></td><td>{row.rules.map(names).join(', ') || 'No detector alert'}</td><td><div className="bulk-source-risk"><span>{names(row.risk_tier)}</span>{row.requires_review && <span className="badge neutral">Context / protected guard</span>}</div></td><td><div className="bulk-state">{row.reference ? <><span className="badge neutral">Reference evaluation complete</span><button className="secondary" onClick={() => setReferenceCase(row)}><span aria-hidden="true">🧪 </span>View reference</button></> : token ? row.reviewed ? 'Frozen human review' : row.triaged ? <><span>Awaiting analyst review</span><button className="secondary" disabled={!!busy} onClick={() => void openReview(row)}><span aria-hidden="true">📝 </span>Review</button></> : row.batch_state ? <><span className="badge neutral">{row.batch_state === 'QUEUED' ? 'Queued for triage' : names(row.batch_state)}</span>{row.batch_id && <button className="secondary" disabled={!!busy} aria-label={'View triage batch for ' + row.name + ' / ' + row.id.slice(0, 6)} onClick={() => navigate(() => { void showBatch(row.batch_id!); })}>View batch</button>}</> : <><span>Pending triage</span><button className="secondary" aria-label={'Triage now for ' + row.name + ' / ' + row.id.slice(0, 6)} disabled={!!busy || !summary || summary.live_triage_available === false || !row.rules.length} onClick={() => navigate(() => { setSelected(row.id); void inspect(true, row); })}><span aria-hidden="true">⚡ </span>Triage now</button></> : predictions[row.id] ? Object.values(predictions[row.id]).map(names).join(', ') : 'Not simulated in this tab'}</div></td><td><button className="secondary case-view-button" aria-label={'View details for ' + row.name + ' / ' + row.id.slice(0, 6)} onClick={() => navigate(() => { setSelected(row.id); setDetailsOpen(true); })}><Icon name="eye" />View details</button></td></tr>)}</tbody></table></div>}
      <div className="bulk-actions"><button className="secondary" disabled={!previous.length} onClick={() => navigate(() => { setCursor(previous[previous.length - 1]); setPrevious(p => p.slice(0, -1)); })}>Previous page</button><button className="secondary" disabled={!page?.next_cursor} onClick={() => navigate(() => { setPrevious(p => [...p, cursor]); setCursor(page!.next_cursor!); })}>Next page</button><span>50 summaries per page. {summary ? activeBaseline ? <>{summary.reference_walkthrough_cases.toLocaleString()} reference + {summary.initially_unprocessed.toLocaleString()} reserved cases in this queue. Reserved reference labels are not displayed.</> : <>{summary.reference_walkthrough_cases.toLocaleString()} reference + {summary.initially_unprocessed.toLocaleString()} operational cases; {(summary.training_transactions - summary.reference_walkthrough_cases).toLocaleString()} additional training and {summary.validation_transactions.toLocaleString()} sealed validation cases stay outside this queue.</> : 'Loading'}</span></div>
    </section>
    {detailsOpen && <Dialog wide title="View details" description="Identity, linked account, source evidence, fixed Python risk weights and available investigation analysis." onClose={() => navigate(() => setDetailsOpen(false))}>{current?.id === selected ? <>
    <section className="panel case-overview"><h2>{current.name}</h2><p>{current.evidence.customer.profile_context}</p><p>{current.alerts.length} source alerts / {current.evidence.history.length} prior transactions in the available 90-day trail.</p>{current.evidence.protected_signals.length > 0 && <p className="protected">Protected signals: {current.evidence.protected_signals.join(', ')}. These require analyst attention regardless of score.</p>}
      <section className="case-risk-details"><h3>Python risk scores and weights</h3><p>Weighted risk score: <strong>{current.risk.overall_score === null ? 'Unavailable' : current.risk.overall_score.toFixed(3)}</strong> / {names(current.risk.tier)}. These fixed risk weights are separate from the machine pre-label cutoffs.</p><div className="table-scroll"><table><thead><tr><th>Dimension</th><th>Score</th><th>Weight</th><th>Weighted contribution</th></tr></thead><tbody>{Object.entries(current.risk.weights).map(([key, weight]) => <tr key={key}><th scope="row">{names(key)}</th><td>{current.risk.dimensions[key]?.toFixed(3) ?? 'Unavailable'}</td><td>{(100 * weight).toFixed(0)}%</td><td>{current.risk.dimensions[key] == null ? 'Unavailable' : (current.risk.dimensions[key]! * weight).toFixed(3)}</td></tr>)}</tbody></table></div><dl className="analysis-labels"><dt>Identity type</dt><dd>{names(current.evidence.customer.customer_type)}</dd><dt>Linked account</dt><dd>{current.evidence.account.id}</dd><dt>Queue hint</dt><dd>{names(current.queue_priority)} / {current.queue_reason}</dd><dt>Recorded signals</dt><dd>{current.evidence.signals.map(names).join(', ') || 'None recorded'}</dd><dt>Missing context</dt><dd>{current.evidence.missing_context.join(', ') || 'None recorded'}</dd></dl></section>
      <details><summary>Source evidence and transaction trail</summary><p>Identity: {current.evidence.customer.display_name} / account {current.evidence.account.id.slice(0, 8)}</p><div className="table-scroll transaction-scroll"><table><thead><tr><th>Date</th><th>Movement</th><th>AUD minor units</th></tr></thead><tbody>{[...current.evidence.history, current.evidence.trigger].map(tx => <tr key={tx.id}><td>{new Date(tx.occurred_at).toLocaleDateString('en-AU')}</td><td>{tx.type}</td><td>{tx.amount_aud_minor.toLocaleString()}</td></tr>)}</tbody></table></div></details>
    <CaseStages current={current} token={token} /></section>
    {token && !current.reference_record && page?.items.some(r => r.id === current.id && !r.reference) && <OperatorReview variant="analysis" key={current.id} current={current} token={token} version={version} onError={onError} onDirty={() => {}} navigate={navigate} onRun={onRun} onComplete={onComplete} onStatus={status} />}
    </> : detailsFailed ? <p role="status">Case details could not be loaded. <button className="secondary" onClick={() => setVersion(value => value + 1)}>Retry case details</button></p> : <p role="status">Loading selected case details...</p>}</Dialog>}
    {token && <p className="muted">Cutoff learning and its controlled exercise are in Evolution lab. Investigations use the existing genuine policy.</p>}
    {token && accounting && <section className="panel"><h2>Genuine bulk usage</h2><UsageDisplay usage={accounting} /></section>}
    {preview && <Dialog title={token ? 'Start genuine case batch?' : 'Run synthetic simulation?'} description={token ? 'The separate worker uses verified authorization and the daily allowance. This creates no human verdict.' : 'This computes heuristic predictions in this tab with zero model requests. It does not create a Gemini investigation.'} onClose={() => setPreview(null)}><p>{preview.rows.length} selected cases / {preview.rows.reduce((n, r) => n + r.rules.length, 0)} generated source alerts / {preview.rows.filter(r => r.requires_review).length} context or protected guards.</p>{preview.live && <p>{preview.live.eligible_cases} eligible cases / {preview.live.alerts} pending alerts / {preview.live.minimum_generation_attempts}-{preview.live.maximum_generation_attempts} generation attempts. Cost estimate unavailable. {preview.live.authorization_available ? '' : 'Live authorization is currently disabled; the job will wait for verified worker access.'}</p>}<button className="primary" onClick={() => void start()} disabled={!!busy}>Start batch</button></Dialog>}
    <ReferenceOverview onError={onError} />
    {reviewCase && token && <Dialog wide title="Analyst review" description={reviewCase.name + ' / inspect the analysis before saving your final verdict.'} onClose={() => navigate(() => { setReviewCase(null); setVersion(v => v + 1); })}><OperatorReview variant="review" key={reviewCase.id} current={reviewCase} token={token} version={version} onError={onError} onDirty={onDirty} navigate={navigate} onRun={onRun} onComplete={onComplete} onStatus={status} initialAlert={reviewCase.alerts.find(a => a.status === 'COMPLETE' && !a.frozen)?.id} /></Dialog>}
    {referenceCase && <Dialog title="Synthetic reference review" description="Preloaded simulation only. This is not a human verdict or a Gemini investigation." onClose={() => setReferenceCase(null)}><h3>{referenceCase.name}</h3><dl className="analysis-labels"><dt>Reference verdict</dt><dd>{names(referenceCase.reference_verdict || '')}</dd><dt>Heuristic pre-label</dt><dd>{names(referenceCase.reference_prediction || '')}</dd><dt>Heuristic score</dt><dd>{referenceCase.reference_score}</dd></dl><p>Baseline cutoffs: TP ≤ 0.25 / FP ≥ 0.65. Protected or missing context forces uncertainty. This reference is read-only and excluded from genuine review counts.</p></Dialog>}
    {confirmation && <Dialog title={confirmation.title} description="This explicit action preserves frozen records and uses revision checks. Synthetic changes apply only to the simulation." onClose={() => setConfirmation(null)}><button className="primary" onClick={() => { const action = confirmation.action; setConfirmation(null); void action(); }}>Confirm action</button></Dialog>}
  </>;
}
