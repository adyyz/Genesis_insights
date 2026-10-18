import React, { useEffect, useRef, useState } from 'react';
import { request } from './api';
import { A0Learning } from './A0Learning';
import { AgentTrace } from './AgentTrace';
import { Dialog } from './Dialog';
import { ReviewOverview } from './ReviewOverview';
import { RuleIntelligence } from './RuleIntelligence';
import { useCorpusSummary } from './useCorpusSummary';
import type { AnalyticalSnapshot, EvolutionDetail, EvolutionRun, Page } from './types';

const base = '/api/operator/bulk/evolution';
const words = (value: string) => value.replaceAll('_', ' ').toLowerCase();
const stages = [['outcomes', '📈', 'Outcomes', 'What have we learned?'], ['understand', '📋', 'Understand', 'Where does uncertainty occur?'], ['learn', '🧬', 'Learn', 'Is a safer cutoff supported?'], ['apply', '⚖️', 'Apply', 'Approve a validated change'], ['observe', '🔎', 'Observe', 'What changed afterward?']];
type Pattern = { id: string; rule_id: string; typology: string; support_feedback_ids: string[]; status: string };
type Observation = { candidate_id: string; rule_id: string; state: string; reassessed_alerts: number; restored_alerts: number; reviewed_alerts: number; true_positive: number; false_positive: number; inconclusive: number; status: string; training: Record<string, number>; validation: { before: Record<string, number>; after: Record<string, number> } | null };
type Props = { discardVersion: number; token: string; selectedRule: string; onSelectRule: (rule: string) => void; onOpenCase: (caseId: string, alertId: string, rule: string) => void; onError: (error: unknown) => void; onDirty: (value: boolean) => void; navigate: (action: () => void) => void; canRun: boolean };

function Stage({ name, children }: { name: string; children: React.ReactNode }) {
  const index = stages.findIndex(item => item[0] === name), [, emoji, label, question] = stages[index];
  return <section id={'evo-' + name} className="evo-stage" aria-labelledby={'evo-title-' + name}>
    <header className="evo-stage-heading"><span className="evo-stage-number" aria-hidden="true">{index + 1}</span><div><h2 id={'evo-title-' + name} tabIndex={-1}><span aria-hidden="true">{emoji} </span>{label}</h2><p>{question}</p></div></header>{children}
  </section>;
}

export function CorpusEvolution({ discardVersion, token, selectedRule, onSelectRule, onOpenCase, onError, onDirty, navigate: guardNavigation, canRun }: Props) {
  const [snapshots, setSnapshots] = useState<Page<AnalyticalSnapshot>>({ items: [], next_cursor: null });
  const [runs, setRuns] = useState<Page<EvolutionRun>>({ items: [], next_cursor: null });
  const [snapshotId, setSnapshotId] = useState(''), [runId, setRunId] = useState(''), [report, setReport] = useState<EvolutionDetail | null>(null);
  const [patterns, setPatterns] = useState<Page<Pattern>>({ items: [], next_cursor: null });
  const [observed, setObserved] = useState<{items: Observation[]; next_cursor: string | null; active_policy: {policy_version_id: string; revision: number}} | null>(null);
  const [version, setVersion] = useState(0), [discard, setDiscard] = useState(0), [busy, setBusy] = useState(false), [loading, setLoading] = useState(true), [reportLoading, setReportLoading] = useState(false);
  const summary = useCorpusSummary(onError, version);
  const [error, setError] = useState(''), [message, setMessage] = useState(''), [confirm, setConfirm] = useState(false);
  const [learningDirty, setLearningDirty] = useState(false), [publicationDirty, setPublicationDirty] = useState(false);
  const [reportError, setReportError] = useState('');
  const lifetime = useRef(new AbortController());
  useEffect(() => () => lifetime.current.abort(), []);
  useEffect(() => { onDirty(learningDirty || publicationDirty); }, [learningDirty, publicationDirty, onDirty]);
  useEffect(() => { setDiscard(value => value + 1); }, [discardVersion]);
  const snapshot = snapshots.items.find(item => item.id === snapshotId);
  const snapshotRun = runs.items.find(item => item.analytical_snapshot_id === snapshotId);
  const matchingReport = report?.analytical_snapshot_id === snapshotId ? report : null;
  const a5Attempt = matchingReport?.agent_trace?.find(item => item.leg_id === 'A5');
  const missingFields = a5Attempt?.validation?.missing_fields;
  function navigate(action: () => void) { guardNavigation(() => { setDiscard(value => value + 1); action(); }); }
  useEffect(() => {
    const abort = new AbortController(); setLoading(true); setError('');
    Promise.all([
      request<Page<AnalyticalSnapshot>>(base + '/snapshots', token, {signal: abort.signal}),
      request<Page<EvolutionRun>>('/api/operator/bulk/reports', token, {signal: abort.signal}),
      request<Page<Pattern>>(base + '/patterns', token, {signal: abort.signal}),
      request<NonNullable<typeof observed>>(base + '/observations', token, {signal: abort.signal})
    ]).then(([inputs, history, memory, effects]) => {
      if (abort.signal.aborted) return;
      setSnapshots(previous => ({...inputs, items: [...inputs.items, ...previous.items.filter(item => item.id === snapshotId && !inputs.items.some(row => row.id === item.id))]}));
      setRuns(previous => ({...history, items: [...history.items, ...previous.items.filter(item => item.id === runId && !history.items.some(row => row.id === item.id))]}));
      setPatterns(memory); setObserved(effects); setSnapshotId(previous => previous || inputs.items[0]?.id || ''); setRunId(previous => previous || history.items[0]?.id || '');
    }).catch(cause => { if (!abort.signal.aborted) { setError('Evolution could not be refreshed. Existing selections are preserved; retry the read.'); onError(cause); } })
      .finally(() => { if (!abort.signal.aborted) setLoading(false); });
    return () => abort.abort();
  }, [token, version]);
  useEffect(() => {
    const abort = new AbortController(); setReport(null); setReportError(''); setReportLoading(!!runId);
    if (runId) request<EvolutionDetail>('/api/operator/bulk/reports/' + runId, token, {signal: abort.signal})
      .then(value => { if (!abort.signal.aborted) setReport(value); }).catch(cause => { if (!abort.signal.aborted) { setReportError('A5 status could not be read. Refresh to check stored state before retrying any work.'); onError(cause); } })
      .finally(() => { if (!abort.signal.aborted) setReportLoading(false); });
    return () => abort.abort();
  }, [runId, token, version]);
  async function perform(action: () => Promise<void>) {
    if (busy) return; setBusy(true); setError(''); setMessage('');
    try { await action(); } catch (cause) { if (!lifetime.current.signal.aborted) { setError('Action could not be confirmed. Refresh stored state before retrying.'); onError(cause); } }
    finally { if (!lifetime.current.signal.aborted) setBusy(false); }
  }
  async function more(kind: 'snapshots' | 'runs' | 'patterns') {
    const page = kind === 'snapshots' ? snapshots : kind === 'runs' ? runs : patterns;
    if (!page.next_cursor) return;
    await perform(async () => {
      const url = kind === 'runs' ? '/api/operator/bulk/reports' : base + '/' + kind;
      const next = await request<Page<AnalyticalSnapshot & EvolutionRun & Pattern>>(url + '?cursor=' + encodeURIComponent(page.next_cursor!), token, {signal: lifetime.current.signal});
      if (lifetime.current.signal.aborted) return;
      if (kind === 'snapshots') setSnapshots(previous => ({...next, items: [...previous.items, ...next.items]}));
      else if (kind === 'runs') setRuns(previous => ({...next, items: [...previous.items, ...next.items]}));
      else setPatterns(previous => ({...next, items: [...previous.items, ...next.items]}));
    });
  }
  return <div className="corpus-evolution">
    <div className="panel evo-scope"><div><span className="eyebrow">GENUINE CORPUS LEARNING</span><h2>From analyst decisions to safer pre-labels.</h2><p>{summary?.cases.toLocaleString() ?? 'Loading'} source cases · {summary?.initially_unprocessed.toLocaleString() ?? 'Loading'} {summary?.scope === 'ACTIVE_BASELINE' ? 'reserved cases' : 'operational cases'}. Only genuine frozen reviews inform this journey. Scores and detectors stay fixed.</p></div><button className="secondary" disabled={loading || busy} onClick={() => setVersion(value => value + 1)}>Refresh corpus Evolution</button></div>
    <nav className="evo-jumps" aria-label="Evolution stages">{stages.map(([id, emoji, label], index) => <button type="button" className="secondary" key={id} onClick={() => { const target = document.getElementById('evo-' + id); target?.scrollIntoView({block: 'start'}); target?.querySelector<HTMLElement>('h2')?.focus({preventScroll: true}); }}><span aria-hidden="true">{emoji} </span>{index + 1}. {label}</button>)}</nav>
    {loading && <p role="status">Loading corpus Evolution…</p>}{error && <p role="status" className="error">{error}</p>}{message && <p role="status">{message}</p>}
    <Stage name="outcomes"><ReviewOverview key={version} token={token} endpoint={base + '/review-summary'} onError={onError} /><p className="evo-stage-note">Learning needs 30 conclusive reviews overall, 15 eligible scored reviews per rule and at least two TP and two FP identities. Learn shows the exact eligible counts; synthetic references and simulated analyst projections never fill these gates.</p></Stage>
    <RuleIntelligence apiBase={base} discardVersion={discard} version={version} token={token} detail={report} selectedRule={selectedRule} onSelectRule={onSelectRule} onOpenCase={onOpenCase} onDirty={setPublicationDirty} navigate={navigate} onError={onError} renderLayout={parts => <>
      <Stage name="understand">{parts.matrix}
        <div className="panel"><h2>Genuine corpus A5 reports</h2><p>Current outcomes above are live reads. Each report below describes its own frozen review sample.</p><div className="form-field"><label htmlFor="corpus-report">Stored genuine bulk A5</label><select id="corpus-report" value={runId} disabled={loading || busy} onChange={event => { const value = event.target.value; navigate(() => setRunId(value)); }}><option value="">No report selected</option>{runs.items.map(run => <option key={run.id} value={run.id}>{words(run.state)} / {run.id.slice(0, 8)}</option>)}</select></div>{runs.next_cursor && <button className="secondary" disabled={busy} onClick={() => void more('runs')}>Load more corpus reports</button>}{reportLoading && <p role="status">Loading frozen report…</p>}</div>{parts.report}
        <details className="panel"><summary>Recurring patterns from frozen genuine reviews</summary><p>Supporting feedback is deduplicated across snapshots. These observations do not authorize a policy change.</p>{patterns.items.length ? <ul>{patterns.items.map(item => <li key={item.id}>{words(item.rule_id)} / {words(item.typology)} · {item.support_feedback_ids.length} supporting reviews · {words(item.status)}</li>)}</ul> : <p>No recurring pattern has been stored.</p>}{patterns.next_cursor && <button className="secondary" disabled={busy} onClick={() => void more('patterns')}>Load more corpus patterns</button>}</details>
      </Stage>
      <A0Learning apiBase={base} discardVersion={discard} token={token} snapshotId={snapshotId} selectedRule={selectedRule} onDirty={setLearningDirty} navigate={navigate} onError={onError} onChanged={() => setVersion(value => value + 1)} renderLayout={learning => <>
        <Stage name="learn"><section className="panel"><h2>Freeze the evidence, then choose an analysis</h2><p>A5 summarizes the frozen evidence with Gemini. Python fits cutoff candidates independently from all eligible frozen reviews, not just A5’s supporting examples.</p><button className="primary" disabled={loading || busy} onClick={() => navigate(() => void perform(async () => { const value = await request<AnalyticalSnapshot>('/api/operator/bulk/snapshots', token, {method: 'POST', signal: lifetime.current.signal}); if (lifetime.current.signal.aborted) return; setSnapshotId(value.id); setVersion(v => v + 1); setMessage('Genuine bulk evidence frozen. Synthetic annotations were excluded.'); }))}>Freeze genuine bulk outcomes</button>
          <div className="form-field"><label htmlFor="corpus-snapshot">Genuine analytical snapshot</label><select id="corpus-snapshot" value={snapshotId} disabled={loading || busy} onChange={event => { const value = event.target.value; navigate(() => setSnapshotId(value)); }}><option value="">No snapshot selected</option>{snapshots.items.map(item => <option key={item.id} value={item.id}>{item.body.sample_sizes.conclusive_reviews} conclusive reviews / {new Date(item.created_at).toLocaleString('en-AU', {timeZone: 'UTC'})} UTC</option>)}</select></div>{snapshots.next_cursor && <button className="secondary" disabled={busy} onClick={() => void more('snapshots')}>Load more corpus snapshots</button>}
          {snapshot && <p className="context-warning">Frozen sample: {snapshot.body.sample_sizes.conclusive_reviews} / 30 conclusive reviews overall; {snapshot.body.detector_gate.target_rule_reviews} / 15 HIGH_VALUE reviews. Cutoff eligibility also requires compatible scores and identity diversity.</p>}
          <button className="secondary" disabled={busy || !snapshot || !canRun} onClick={() => setConfirm(true)}>Run genuine bulk A5</button>{!canRun && <p className="muted">Verified model access is required for A5. Deterministic cutoff preparation sends no model request.</p>}
          <section className="a5-run-status" data-state={matchingReport?.run.state} aria-labelledby="corpus-a5-status">
            <div className="panel-heading"><h3 id="corpus-a5-status">A5 run status</h3><button className="secondary" disabled={loading || reportLoading || busy} onClick={() => setVersion(value => value + 1)}>Refresh A5 status</button></div>
            <p className="muted">Selected frozen snapshot. Refresh checks progress without starting another request.</p>
            {reportLoading ? <p role="status">Reading A5 run status...</p> : reportError ? <p role="status" className="error">{reportError}</p> : matchingReport ? <>
              <div className="a5-status-labels" role="status"><span className="badge neutral">{words(matchingReport.run.state)}</span>{matchingReport.run.error_code && <code>{matchingReport.run.error_code}</code>}</div>
              <dl className="a5-run-metadata"><div><dt>Run</dt><dd title={matchingReport.run.id}>{matchingReport.run.id.slice(0, 8)}</dd></div><div><dt>Stage</dt><dd>{matchingReport.run.stage || 'Not started'}</dd></div><div><dt>Model attempts</dt><dd>{a5Attempt?.attempts ?? 'Not recorded'}</dd></div></dl>
              <p className={matchingReport.run.state === 'FAILED' ? 'a5-failure-detail' : ''}>{matchingReport.run.state === 'FAILED' ? a5Attempt?.validation?.error_types?.includes('missing') ? `Gemini returned a report with missing required fields${missingFields?.length ? ': ' + missingFields.join(', ') : ' (field names were not recorded)'}. It was rejected; no report or policy change was saved.` : matchingReport.run.error_code === 'PROVIDER_HTTP_400' ? 'Gemini rejected the request configuration. No validated report was saved.' : 'The analysis failed validation or execution. No validated report or policy change was saved. See the recorded trace below.' : matchingReport.run.state === 'QUEUED' ? 'Waiting for the separate worker. No completed analysis is available yet.' : matchingReport.run.state === 'RUNNING' ? 'The worker is processing this frozen snapshot. Refresh explicitly to check progress.' : matchingReport.run.state === 'COMPLETE' ? 'Execution completed. Review the analytical result separately; completion does not establish learning eligibility.' : 'This is the recorded execution state; no completed analysis is assumed.'}</p>
              {matchingReport.result && <p><strong>Analytical result: {words(matchingReport.result.status)}</strong><br />{matchingReport.result.summary}</p>}
              <AgentTrace detail={matchingReport} />
            </> : snapshotRun ? <><p>A stored run is available for this snapshot. The report selection currently refers to another run.</p><button className="secondary" disabled={busy} onClick={() => navigate(() => setRunId(snapshotRun.id))}>Show this snapshot's A5 status</button></> : <p>{snapshot ? 'No A5 run for this snapshot appears in the loaded history. Load more corpus reports to check older runs.' : 'Select a frozen snapshot to inspect its A5 run.'}</p>}
          </section></section>{learning.learn}
        </Stage>
        <Stage name="apply"><p className="evo-stage-note">Only A0 cutoffs can be applied here. Approval alone changes nothing. Detector recommendations are informational; archived detector controls retain their original scope.</p>{learning.apply}</Stage>
        <Stage name="observe"><section className="panel"><h2>Observed corpus effects</h2><p>Active A0 policy revision: {observed?.active_policy.revision ?? 'Unavailable'}. Refresh explicitly after application, reassessment or new reviews.</p>{!observed ? <p>Observed effects are unavailable until the read succeeds.</p> : !observed.items.length ? <p>Not yet observed. No corpus cutoff application has been recorded.</p> : observed.items.map(item => <article className="analysis-block" key={item.candidate_id}><h3>{words(item.rule_id)} / {words(item.state)}</h3><dl className="usage-values"><dt>Training: correct FP uncertainty resolutions</dt><dd>{item.training.correct_fp_resolutions}</dd><dt>Prospective validation: correct FP uncertainty resolutions</dt><dd>{item.validation?.after.correct_fp_resolutions ?? 'Not recorded'}</dd><dt>Explicitly reassessed / restored alerts</dt><dd>{item.reassessed_alerts} / {item.restored_alerts}</dd><dt>Subsequent human reviews</dt><dd>{item.status === 'NOT_YET_OBSERVED' ? 'Not yet observed' : `${item.reviewed_alerts}: ${item.true_positive} TP / ${item.false_positive} FP / ${item.inconclusive} inconclusive`}</dd></dl></article>)}<p className="muted">Reassessed labels are not human reviews. Training and validation outcomes do not prove a causal production improvement.</p>{observed?.next_cursor && <button className="secondary" disabled={busy} onClick={() => void perform(async () => { const next = await request<NonNullable<typeof observed>>(base + '/observations?cursor=' + encodeURIComponent(observed.next_cursor!), token, {signal: lifetime.current.signal}); if (!lifetime.current.signal.aborted) setObserved(previous => previous ? {...next, items: [...previous.items, ...next.items]} : next); })}>Load more observed applications</button>}</section>{parts.publication}</Stage>
      </>} />
    </>} />
    {confirm && <Dialog title="Run genuine A5 on this snapshot?" description="This explicitly queues a Gemini analysis of the selected frozen genuine reviews through the existing worker and usage controls. It does not prepare or apply cutoffs." onClose={() => setConfirm(false)}><button className="primary" disabled={busy} onClick={() => { setConfirm(false); void perform(async () => { const value = await request<EvolutionDetail>('/api/operator/bulk/reports', token, {method: 'POST', body: JSON.stringify({snapshot_id: snapshotId}), signal: lifetime.current.signal}); if (lifetime.current.signal.aborted) return; setRunId(value.run.id); setVersion(v => v + 1); setMessage('A5 request recorded. Refresh corpus Evolution after the separate worker finishes.'); }); }}>Confirm action</button></Dialog>}
  </div>;
}
