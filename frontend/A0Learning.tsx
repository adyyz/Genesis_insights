import React, { useEffect, useState } from 'react';
import { request } from './api';
import { Dialog } from './Dialog';
import type { Page } from './types';

type Candidate = { id: string; rule_id: string; state: string; revision: number; baseline: { tp: number; fp: number }; thresholds: { tp: number; fp: number }; frozen_at: string; before: Record<string, number>; after: Record<string, number>; training: { identity_id: string }[]; training_count?: number; identity_count?: number; applied_policy_id?: string; previous_policy_id?: string; validation: { passed: boolean; before: Record<string, number>; after: Record<string, number> } | null; validation_progress?: number };
type Calibration = { id: string; overall_reviews: number; results: { rule_id: string; sample_size: number; identity_count: number; verdict_identity_counts?: Record<string, number>; status: string }[] };
type Result = { items: { alert_id: string; state: string; reason?: string }[]; next_cursor: string | null };
type Action = 'prepare' | 'validate' | 'approve' | 'reject' | 'apply' | 'rollback' | 'reassess';
type Props = { apiBase?: string; renderLayout?: (parts: { learn: React.ReactNode; apply: React.ReactNode }) => React.ReactNode; discardVersion: number; token: string; snapshotId: string; selectedRule: string; onDirty: (value: boolean) => void; navigate: (action: () => void) => void; onError: (error: unknown) => void; onChanged: () => void };
const words = (text: string) => text.replaceAll('_', ' ').toLowerCase();

export function A0Learning({ apiBase = '/api/operator/bulk/evolution', renderLayout, discardVersion, token, snapshotId, selectedRule, onDirty, navigate, onError, onChanged }: Props) {
  const fieldPrefix = 'corpus-';
  const [candidates, setCandidates] = useState<Page<Candidate>>({ items: [], next_cursor: null });
  const [calibrations, setCalibrations] = useState<Calibration[]>([]), [selected, setSelected] = useState(''), [notes, setNotes] = useState('');
  const [policy, setPolicy] = useState<{ policy_version_id: string; revision: number; rules?: Record<string, {tp: number; fp: number}> }>({ policy_version_id: 'auto-label-v1', revision: 0 }), [refresh, setRefresh] = useState(0), [busy, setBusy] = useState(false), [loading, setLoading] = useState(true), [failed, setFailed] = useState(false);
  const [confirm, setConfirm] = useState<Action | null>(null), [message, setMessage] = useState(''), [batch, setBatch] = useState<Result | null>(null), [progress, setProgress] = useState<number | null>(null);
  const row = candidates.items.find(item => item.id === selected);
  const eligible = candidates.items.filter(item => item.rule_id === selectedRule);
  useEffect(() => { setNotes(''); }, [discardVersion]);
  useEffect(() => { onDirty(!!notes); }, [notes, onDirty]);
  useEffect(() => { setBatch(null); setProgress(null); }, [selectedRule, selected]);
  useEffect(() => {
    const controller = new AbortController(); setLoading(true); setFailed(false);
    Promise.all([request<Page<Candidate> & { active_policy: typeof policy }>(apiBase + '/auto-label/candidates', token, { signal: controller.signal }), request<Page<Calibration>>(apiBase + '/auto-label/calibrations?limit=5', token, { signal: controller.signal })])
      .then(([items, runs]) => { if (!controller.signal.aborted) { setCandidates(items); setPolicy(items.active_policy); setCalibrations(runs.items); setSelected(previous => items.items.some(item => item.id === previous && item.rule_id === selectedRule) ? previous : items.items.find(item => item.rule_id === selectedRule)?.id || ''); } })
      .catch(error => { if (!controller.signal.aborted) { setFailed(true); onError(error); } }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [token, refresh, selectedRule, apiBase]);
  async function more() {
    if (!candidates.next_cursor) return;
    try { const next = await request<Page<Candidate>>(`${apiBase}/auto-label/candidates?cursor=${encodeURIComponent(candidates.next_cursor)}`, token); setCandidates(previous => ({ ...next, items: [...previous.items, ...next.items] })); }
    catch (error) { onError(error); }
  }
  function prepare(action: Action) {
    if (!['prepare', 'validate'].includes(action) && !notes.trim()) { setMessage('Add decision notes before continuing.'); return; }
    setMessage(''); setConfirm(action);
  }
  async function execute(action: Action) {
    if (busy || (action !== 'prepare' && !row)) return;
    setBusy(true); setMessage('');
    try {
      if (action === 'prepare') {
        await request(apiBase + '/auto-label/calibrations', token, { method: 'POST', body: JSON.stringify({ snapshot_id: snapshotId }) });
        setMessage('Learning run stored. Insufficient samples and no-change results are shown honestly; no policy was applied.');
      } else {
        const suffix = ['approve', 'reject'].includes(action) ? 'review' : action;
        const body = action === 'validate' ? {} : action === 'reassess' ? { expected_revision: row!.revision, expected_policy_version: policy.policy_version_id, notes: notes.trim(), cursor: batch?.next_cursor || null } : { expected_revision: row!.revision, notes: notes.trim(), ...(['apply', 'rollback'].includes(action) ? { expected_policy_revision: policy.revision } : { state: action === 'approve' ? 'APPROVED' : 'REJECTED' }) };
        const value = await request<Candidate & Result>(`${apiBase}/auto-label/candidates/${row!.id}/${suffix}`, token, { method: suffix === 'review' ? 'PUT' : 'POST', body: JSON.stringify(body) });
        if (action === 'validate') { setProgress(value.validation_progress ?? null); setMessage(value.validation ? `Prospective validation ${value.validation.passed ? 'passed' : 'did not establish safe benefit'}.` : `Awaiting new independent reviews: ${value.validation_progress || 0} / 5.`); }
        else if (action === 'reassess') { setBatch(value); setMessage(`${value.items.filter(item => item.state === 'SUCCEEDED').length} assessments available; ${value.items.filter(item => item.state === 'SKIPPED').length} skipped. No model request. Refresh the investigation queue to inspect results.`); }
        else { setBatch(null); setProgress(null); setMessage(action === 'rollback' ? 'Prior A0 policy restored for new work. Reassess affected unreviewed alerts explicitly below.' : 'A0 decision recorded. Approval alone does not activate a policy.'); }
      }
      setConfirm(null); setNotes(''); setRefresh(value => value + 1); onChanged();
    } catch (error) { setMessage('Action failed or state changed. Notes are preserved; refresh authoritative state before retrying.'); onError(error); }
    finally { setBusy(false); }
  }
  const learnPanel = <section className="panel a0-learning"><div className="panel-heading"><h2><span aria-hidden="true">⚖️ </span>A0 threshold learning</h2><button className="text-button" disabled={busy || loading} onClick={() => setRefresh(value => value + 1)}>Refresh A0 learning</button></div>
    <p>Prepare eligible rules together using genuine human reviews. A0 cutoffs change machine pre-labels; they do not remove detector alerts or decide human verdicts.</p><p className="muted">30 conclusive reviews overall / 15 compatible scored reviews per rule / at least two TP and two FP identities. Candidates require five new independent identity reviews after freeze, both verdict classes and safe measured benefit. Protected signals remain uncertain.</p>
    <button className="primary" disabled={busy || loading || !snapshotId} onClick={() => prepare('prepare')}>Prepare eligible A0 rules</button>{!snapshotId && <p className="muted">Freeze and select genuine analytical inputs first.</p>}
    {loading && <p role="status">Loading A0 learning…</p>}{failed && <p role="status">A0 learning could not be loaded. Refresh to retry.</p>}
    {calibrations[0] && <details><summary>Latest learning run / {calibrations[0].overall_reviews} conclusive reviews</summary><div className="table-scroll"><table><thead><tr><th>Rule</th><th>Scored reviews / identities</th><th>TP / FP identities</th><th>Outcome</th></tr></thead><tbody>{calibrations[0].results.map(item => <tr key={item.rule_id}><th scope="row">{words(item.rule_id)}</th><td>{item.sample_size} / {item.identity_count}</td><td>{item.verdict_identity_counts ? `${item.verdict_identity_counts.TRUE_POSITIVE} / ${item.verdict_identity_counts.FALSE_POSITIVE}` : "Not recorded"}</td><td>{words(item.status)}</td></tr>)}</tbody></table></div></details>}
    <div className="form-field"><label htmlFor={fieldPrefix + 'a0-candidate'}>A0 candidate for {words(selectedRule)}</label><select id={fieldPrefix + 'a0-candidate'} value={selected} disabled={busy || loading} onChange={event => { const value = event.target.value; navigate(() => { setSelected(value); setNotes(''); setBatch(null); setProgress(null); }); }}><option value="">No candidate selected</option>{eligible.map(item => <option key={item.id} value={item.id}>{words(item.rule_id)} / {words(item.state)}</option>)}</select></div>
    {!eligible.length && !loading && <p>No eligible candidate recorded for this rule. Collect genuine evidence; no improvement is assumed.</p>}
    {row && <article><h3>{words(row.rule_id)} / {words(row.state)}</h3><dl className="usage-values"><dt>TP cutoff / before → proposed</dt><dd>{row.baseline.tp} → {row.thresholds.tp}</dd><dt>FP cutoff / before → proposed</dt><dd>{row.baseline.fp} → {row.thresholds.fp}</dd><dt>Training reviews / identities</dt><dd>{row.training_count ?? row.training.length} / {row.identity_count ?? new Set(row.training.map(item => item.identity_id)).size}</dd><dt>Frozen</dt><dd>{new Date(row.frozen_at).toLocaleString('en-AU', { timeZone: 'UTC' })} UTC</dd><dt>Forward validation</dt><dd>{row.validation ? row.validation.passed ? 'Passed on 5 new identities' : 'Failed / no safe measured benefit' : progress === null ? "Not checked since refresh" : `${progress} / 5 new identities observed`}</dd></dl>
      {row.validation && <div className="table-scroll"><table><caption>Prospective human-reviewed observations / not population accuracy</caption><thead><tr><th>Measure</th><th>Before</th><th>Candidate</th></tr></thead><tbody>{Object.entries({ wrong_fp: 'Human TP labeled AUTO_FP', wrong_tp: 'Human FP labeled AUTO_TP', correct_fp_resolutions: 'Uncertain human FP correctly resolved', uncertain: 'Still uncertain' }).map(([key, label]) => <tr key={key}><th scope="row">{label}</th><td>{row.validation!.before[key]}</td><td>{row.validation!.after[key]}</td></tr>)}</tbody></table></div>}
      <button className="text-button" disabled={busy || row.state !== 'PROPOSED' || !!row.validation} onClick={() => prepare('validate')}>Check new validation reviews</button>
    </article>}
    {candidates.next_cursor && <button className="text-button" disabled={busy} onClick={() => void more()}>Load more A0 candidates</button>}
</section>;
  const applyPanel = <section className="panel a0-application"><h2>Review and apply A0 cutoffs</h2>
{row ? <article><h3>{words(row.rule_id)} / {words(row.state)}</h3><p>Current policy revision {policy.revision}{policy.rules?.[row.rule_id] && ` (TP <= ${policy.rules[row.rule_id].tp}, FP >= ${policy.rules[row.rule_id].fp})`}. Original to candidate: TP cutoff {row.baseline.tp} → {row.thresholds.tp}; FP cutoff {row.baseline.fp} → {row.thresholds.fp}. {row.validation?.passed ? 'Prospective validation passed.' : 'Application awaits passing prospective validation.'}</p>      <div className="form-field"><label htmlFor={fieldPrefix + 'a0-decision-notes'}>A0 decision notes</label><textarea className="resize-none" id={fieldPrefix + 'a0-decision-notes'} rows={3} maxLength={2000} value={notes} disabled={busy} onChange={event => setNotes(event.target.value)} /></div>
      <div className="dialog-actions"><button className="primary" disabled={busy || row.state !== 'PROPOSED' || !row.validation?.passed} onClick={() => prepare('approve')}>Approve A0 candidate</button><button className="text-button" disabled={busy || row.state !== 'PROPOSED'} onClick={() => prepare('reject')}>Reject A0 candidate</button><button className="primary" disabled={busy || row.state !== 'APPROVED'} onClick={() => prepare('apply')}>Apply A0 cutoffs</button><button className="text-button" disabled={busy || row.state !== 'APPLIED' || policy.policy_version_id !== row.applied_policy_id} onClick={() => prepare('rollback')}>Roll back A0 policy</button><button className="text-button" disabled={busy || !['APPLIED', 'ROLLED_BACK'].includes(row.state)} onClick={() => prepare('reassess')}>{row.state === 'ROLLED_BACK' ? 'Restore affected assessments' : batch?.next_cursor ? 'Continue uncertain reassessment' : 'Reassess uncertain alerts'}</button></div>
      {batch && <p>{batch.items.filter(item => item.state === 'SUCCEEDED').length} succeeded / {batch.items.filter(item => item.state === 'SKIPPED').length} skipped in the last batch. {batch.next_cursor ? 'More records remain.' : 'Batch traversal complete.'}</p>}
</article> : <p>No candidate is selected. Prepare and validate eligible genuine evidence in Learn before applying a change.</p>}</section>;
  return <>
    {renderLayout ? renderLayout({ learn: learnPanel, apply: applyPanel }) : <>{learnPanel}{applyPanel}</>}
    {message && <p role="status">{message}</p>}
    {confirm && <Dialog title="Confirm A0 learning action" description={confirm === 'prepare' ? 'Freeze deterministic candidates for all eligible rules from the selected snapshot. No model call or policy activation occurs.' : confirm === 'reassess' ? 'Process up to 50 records using stored evidence. Frozen, in-progress and incompatible alerts are skipped; historical output is preserved.' : `Record ${confirm} for the selected candidate. Approval and application are separate, revision-checked actions.`} onClose={() => setConfirm(null)}>{message && <p role="status">{message}</p>}<div className="dialog-actions"><button className="text-button" disabled={busy} onClick={() => setConfirm(null)}>Keep reviewing</button><button className="primary" disabled={busy} onClick={() => void execute(confirm)}>Confirm A0 action</button></div></Dialog>}
  </>;
}
