import React, { useEffect, useState } from 'react';
import { AgentTrace } from './AgentTrace';
import { Icon } from './Icon';
import { request } from './api';
import type { CaseRecord, Finding, PrivateAlert, FeedbackEvent, Page, RunDetail } from './types';

export function OperatorReview({ current, token, version, onError, onDirty, navigate, onRun, onComplete, onStatus, initialAlert, variant = 'full' }: { current: CaseRecord; token: string; version: number; onError: (error: unknown) => void; onDirty: (dirty: boolean) => void; navigate: (action: () => void) => void; onRun: (id: string) => void; onComplete: (run: RunDetail) => void; onStatus: (alert: PrivateAlert['alert']) => void; initialAlert?: string; variant?: 'full' | 'analysis' | 'review' }) {
  const [selected, setSelected] = useState(current.alerts.find(alert => alert.id === initialAlert)?.id || current.alerts[0]?.id || '');
  const [detail, setDetail] = useState<PrivateAlert | null>(null);
  const [frozenSelection, setFrozenSelection] = useState('');
  const selectedRef = React.useRef(selected); selectedRef.current = selected;
  const [refresh, setRefresh] = useState(0);
  const [loading, setLoading] = useState(false);
  const [history, setHistory] = useState<Page<FeedbackEvent>>({ items: [], next_cursor: null });
  const [verdict, setVerdict] = useState('INCONCLUSIVE');
  const [notes, setNotes] = useState('');
  const [override, setOverride] = useState('');
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState('');
  const [fieldError, setFieldError] = useState('');
  const notesRef = React.useRef<HTMLTextAreaElement>(null);
  const complete = detail?.alert.status === 'COMPLETE' && !!detail.alert.current_run_id;
  const frozen = (detail?.alert.feedback_revision || 0) > 0 || frozenSelection === selected;
  const machineLabel = detail?.effective_assessment?.body.predicted_verdict || detail?.outputs.A0?.predicted_verdict;
  const suggestion = machineLabel === 'AUTO_TP' ? 'TRUE_POSITIVE' : machineLabel === 'AUTO_FP' ? 'FALSE_POSITIVE' : machineLabel === 'AUTO_UNCERTAIN' ? 'INCONCLUSIVE' : undefined;
  useEffect(() => { onDirty(variant !== 'analysis' && !frozen && (!!notes || override !== '' || verdict !== 'INCONCLUSIVE')); }, [notes, override, verdict, frozen, onDirty]);
  useEffect(() => { setFrozenSelection(''); setNotes(''); setOverride(''); setVerdict('INCONCLUSIVE'); setMessage(''); setFieldError(''); }, [selected, token]);
  useEffect(() => {
    const controller = new AbortController();
    setDetail(null); setHistory({ items: [], next_cursor: null });
    if (selected && token) {
      setLoading(true);
      Promise.all([
        request<PrivateAlert>(`/api/operator/alerts/${selected}`, token, { signal: controller.signal }),
        request<Page<FeedbackEvent>>(`/api/operator/alerts/${selected}/feedback`, token, { signal: controller.signal })
      ]).then(async ([alert, events]) => {
        if (controller.signal.aborted) return;
        setDetail(alert); setHistory(events); onStatus(alert.alert);
        if (alert.alert.feedback_revision > 0 && events.items[0]) { setFrozenSelection(selected); setVerdict(events.items[0].verdict); setNotes(events.items[0].notes); setOverride(''); }
        if (refresh > 0 && alert.latest_run?.parent_run_id) {
          const run = await request<RunDetail>(`/api/operator/runs/${alert.latest_run.parent_run_id}`, token, { signal: controller.signal });
          if (!controller.signal.aborted) onComplete(run);
        }
      })
        .catch(error => { if (!controller.signal.aborted) onError(error); })
        .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    }
    return () => controller.abort();
  }, [selected, token, version, refresh]);
  async function submit(event: React.FormEvent) {
    event.preventDefault(); if (!complete || frozen || saving) return;
    if (!notes.trim()) { setFieldError('Add your review notes.'); notesRef.current?.focus(); return; }
    setSaving(true); setFieldError(''); setMessage('');
    try {
      const saved = await request<FeedbackEvent>(`/api/operator/alerts/${selected}/feedback`, token, { method: 'POST', body: JSON.stringify({ verdict, notes: notes.trim(), override_action: override || null, expected_revision: detail!.alert.feedback_revision, expected_assessment_id: detail?.effective_assessment?.id || null }) });
      onStatus({ ...detail!.alert, feedback_revision: saved.revision }); setFrozenSelection(selected); setDetail(previous => previous && ({ ...previous, alert: { ...previous.alert, feedback_revision: saved.revision } })); setHistory(previous => ({ ...previous, items: [saved, ...previous.items] })); onDirty(false); setMessage('Feedback recorded. Your verdict is frozen.'); setRefresh(value => value + 1);
    } catch (error) { setMessage((error as { status?: number }).status === 409 ? 'Feedback changed or triage is incomplete. Refresh status before submitting again.' : 'Feedback was not saved. Your notes are preserved.'); onError(error); }
    finally { setSaving(false); }
  }
  async function moreHistory() {
    if (!history.next_cursor || loading) return;
    const requestedAlert = selected;
    setLoading(true);
    try {
      const next = await request<Page<FeedbackEvent>>(`/api/operator/alerts/${selected}/feedback?cursor=${encodeURIComponent(history.next_cursor)}`, token);
      if (selectedRef.current !== requestedAlert) return;
      setHistory(previous => ({ ...next, items: [...previous.items, ...next.items] }));
    } catch (error) { onError(error); } finally { setLoading(false); }
  }
  if (!token || !current.alerts.length) return null;
  return <section className="panel operator-review"><div className="panel-heading"><h3>Private alert status</h3><button className="secondary" disabled={loading} onClick={() => setRefresh(refresh + 1)}>Refresh status</button></div>
    <label htmlFor="review-alert">Selected alert</label><select id="review-alert" value={selected} disabled={saving} onChange={event => { const id = event.target.value; navigate(() => setSelected(id)); }}>{current.alerts.map(alert => <option key={alert.id} value={alert.id}>{alert.rule_name}</option>)}</select>
    {loading ? <p role="status">Loading private alert…</p> : detail && <><p>Alert: {detail.alert.status}. Latest run: {detail.latest_run?.state || 'Not run'}.</p>{detail.latest_run?.error_code && <p role="status">{detail.latest_run.error_code}: {detail.latest_run.error_code === 'AI_NOT_CONNECTED' ? 'Live access was unavailable for a step. Inspect the sealed steps for any earlier outcomes.' : 'Check the stored run details before continuing.'}</p>}</>}
    {variant !== 'review' && detail?.latest_run?.parent_run_id && <button className="secondary" onClick={() => onRun(detail.latest_run!.parent_run_id!)}>View run usage</button>}
    {variant !== 'review' && detail && <AgentTrace detail={detail} />}
    {variant !== 'review' && complete && detail && <ValidatedInvestigation detail={detail} current={current} />}
    {variant !== 'analysis' && <><div className="review-section-heading"><span className="eyebrow">DECIDE</span><h3><span aria-hidden="true">📝 </span>Analyst review</h3></div><p id="feedback-gate">{frozen ? 'Your verdict is frozen. Choose another alert or identity to continue.' : complete ? 'Save feedback once to freeze your final verdict for this alert.' : 'A completed, validated AI investigation is required before feedback.'}</p>
    {frozen ? <article className="frozen-review" aria-label="Saved analyst verdict"><div><span className="stage-complete"><Icon name="check" size={15} /> Saved and frozen</span><strong>{verdict.replaceAll('_', ' ').toLowerCase()}</strong></div><details><summary>View saved review</summary><p>{history.items[0]?.notes || notes}</p>{history.items[0]?.override_action && <p><strong>Action override:</strong> {history.items[0].override_action?.toLowerCase()}</p>}</details></article> : <form noValidate onSubmit={submit} aria-describedby="feedback-gate"><fieldset disabled={!complete || frozen || saving || loading}>
      {suggestion && <><p><strong>Machine suggestion:</strong> {machineLabel?.replaceAll('_', ' ').toLowerCase()} / heuristic score {detail?.effective_assessment?.body.heuristic_score ?? detail?.outputs.A0?.heuristic_score ?? 'Not recorded'}. This is not a calibrated probability.</p><div className="dialog-actions"><button className="secondary" type="button" onClick={() => setVerdict(suggestion)}><span aria-hidden="true">🤝 </span>Agree with AI suggestion</button><button className="secondary" type="button" onClick={() => { setVerdict('INCONCLUSIVE'); document.getElementById('review-verdict')?.focus(); }}><span aria-hidden="true">📝 </span>Choose my own verdict</button></div></>}
      <div className="form-field"><label htmlFor="review-verdict">Verdict</label><select id="review-verdict" value={verdict} onChange={event => setVerdict(event.target.value)}><option value="INCONCLUSIVE">Inconclusive</option><option value="TRUE_POSITIVE">True positive</option><option value="FALSE_POSITIVE">False positive</option></select></div>
      <div className="form-field"><label htmlFor="review-notes">Review notes</label><textarea className="resize-none" id="review-notes" ref={notesRef} rows={5} maxLength={2000} required value={notes} aria-invalid={!!fieldError} aria-describedby={fieldError ? 'review-notes-error' : undefined} onChange={event => setNotes(event.target.value)} />{fieldError && <span id="review-notes-error" className="field-error">{fieldError}</span>}</div>
      <div className="form-field"><label htmlFor="review-override">Action override (optional)</label><select id="review-override" value={override} onChange={event => setOverride(event.target.value)}><option value="">No override</option>{['ESCALATE', 'REVIEW', 'MONITOR', 'CLOSE'].map(action => <option key={action}>{action}</option>)}</select></div>
      <button className="primary" disabled={!complete || frozen || saving || loading} aria-busy={saving}>Save feedback</button>
    </fieldset></form>}{message && <p role="status">{message}</p>}
    <h3>Feedback history</h3>{history.items.length ? <ol className="feedback-history" tabIndex={0} aria-label="Feedback history">{history.items.map(event => <li key={event.id}><strong>{event.verdict.replaceAll('_', ' ').toLowerCase()}</strong><p>{event.notes}</p><small>Revision {event.revision} · {event.provenance} · {new Date(event.submitted_at).toLocaleString('en-AU', { timeZone: 'UTC' })} UTC</small></li>)}</ol> : <p className="muted">No feedback recorded for this alert.</p>}
    {history.next_cursor && <button className="secondary" disabled={loading} onClick={() => void moreHistory()}>Load more feedback</button>}</>}
  </section>;
}

function Findings({ title, items, current }: { title: React.ReactNode; items: Finding[]; current: CaseRecord }) {
  const sources = [current.evidence.trigger, ...[...current.evidence.history].reverse()];
  function reference(id: string) {
    const index = sources.findIndex(source => source.id === id);
    return index < 0 ? <code title={id}>{id.slice(0, 8)}...</code>
      : <a href={`#transaction-${id}`}>{index === 0 ? 'Trigger transaction' : `History ${index}`}</a>;
  }
  function narrative(text: string) {
    return text.split(/([a-f0-9]{24,64})/g).map((part, index) => /^[a-f0-9]{24,64}$/.test(part)
      ? <React.Fragment key={index}>{reference(part)}</React.Fragment> : part);
  }
  return <section className="analysis-block"><h4>{title}</h4>{items.length ? <ul>{items.map((finding, index) => <li key={index}><p>{narrative(finding.text)}</p>{finding.evidence_ids.length > 0 && <details className="source-references"><summary>Sources ({finding.evidence_ids.length})</summary><div>{finding.evidence_ids.map(id => <span key={id}>{reference(id)}</span>)}</div></details>}</li>)}</ul> : <p className="muted">None recorded in this model output.</p>}</section>;
}


function InvestigationBrief({ brief, current }: { brief: NonNullable<PrivateAlert['outputs']['A4']>; current: CaseRecord }) {
  const title = (icon: string, text: string) => <><span aria-hidden="true">{icon} </span>{text}</>;
  const notes = (heading: string, values: string[]) => <section className="analysis-block"><h4>{heading}</h4>{values.length ? <ul>{values.map((text, index) => <li key={index}>{text}</li>)}</ul> : <p className="muted">None recorded.</p>}</section>;
  return <section className="brief-narrative" aria-label="Investigation brief">
    <div className="brief-lead" data-tone="overview"><span className="brief-step" aria-hidden="true">1</span><Findings current={current} title={title('📋', 'Overview')} items={[brief.overview]} /></div>
    <div className="brief-lead" data-tone="findings"><span className="brief-step" aria-hidden="true">2</span><Findings current={current} title={title('🔎', 'Key findings')} items={brief.key_findings} /></div>
    <ol className="brief-snake" start={3} aria-label="Evidence to action trail">
      <li data-tone="counter"><span className="brief-step" aria-hidden="true">3</span><Findings current={current} title={title('⚖️', 'Counter-evidence')} items={brief.counter_evidence} /></li>
      <li data-tone="missing"><span className="brief-step" aria-hidden="true">4</span>{notes('Missing information', brief.missing_information)}</li>
      <li data-tone="actions"><span className="brief-step" aria-hidden="true">5</span>{notes('Next steps', brief.next_steps)}</li>
      <li data-tone="limits"><span className="brief-step" aria-hidden="true">6</span>{notes('Limitations', brief.limitations)}</li>
    </ol>
  </section>;
}

function ValidatedInvestigation({ detail, current }: { detail: PrivateAlert; current: CaseRecord }) {
  const { A0: sealedA0, A2, A3, A4 } = detail.outputs;
  const A0 = detail.effective_assessment ? { ...sealedA0, ...detail.effective_assessment.body } : sealedA0;
  return <article className="validated-investigation"><div className="analysis-heading"><span className="eyebrow">INVESTIGATION RESULT</span><h3><span aria-hidden="true">🤖 </span>AI analysis and suggested actions</h3></div><p className="muted">Validated model suggestions, not an analyst verdict. Source-ID checks do not establish semantic support; inspect the cited evidence before deciding.</p>
    {A4 && <InvestigationBrief current={current} brief={A4} />}
    <dl className="analysis-labels"><dt>Machine pre-label</dt><dd>{A0?.predicted_verdict ? A0.predicted_verdict.replaceAll('_', ' ').toLowerCase() : 'Not recorded in this run'}</dd><dt>Heuristic score</dt><dd>{A0?.heuristic_score ?? 'Unavailable'}</dd></dl><p className="muted">A deterministic suggestion, not a calibrated probability or analyst verdict. Policy: {A0?.label_policy_version || 'Legacy priority only'}.</p>{A0?.label_signals?.length ? <p>Signals: {A0.label_signals.join(', ')}</p> : null}
    {detail.alert.priority && <p>Post-triage priority: {detail.alert.priority.replaceAll('_', ' ').toLowerCase()}</p>}
    {detail.advisory && <section className="analysis-block"><h4>AI advisory risk opinion</h4><p><strong>Python score (authoritative):</strong> {detail.advisory.authoritative_score ?? 'Unavailable'} / <strong>AI opinion:</strong> {detail.advisory.overall_score ?? 'Not recorded or unavailable'}</p>{detail.advisory.dimensions && <details><summary>Compare risk dimensions</summary><div className="table-scroll"><table><thead><tr><th>Dimension</th><th>Python / authoritative</th><th>AI / advisory</th><th>Weight</th></tr></thead><tbody>{Object.entries(detail.advisory.dimensions).map(([name, value]) => <tr key={name}><th scope="row">{name}</th><td>{current.risk.dimensions[name] ?? "Unavailable"}</td><td>{value ?? "Unavailable"}</td><td>{current.risk.weights[name]}</td></tr>)}</tbody></table></div></details>}{detail.advisory.large_difference && <p className="context-warning">The AI opinion differs by more than 0.15. Inspect the supporting evidence; the Python score is unchanged.</p>}</section>}
    {A3 && <><dl className="analysis-labels"><dt>Suggested typology</dt><dd>{A3.primary_typology.replaceAll('_', ' ').toLowerCase()}</dd><dt>Suggested action</dt><dd>{A3.suggested_action.toLowerCase()}</dd><dt>Uncertainty</dt><dd>{A3.uncertainty.toLowerCase()}</dd></dl><Findings current={current} title="Classification rationale" items={[A3.rationale]} />{A3.secondary_typologies === undefined && <p className="muted">Secondary typologies: Not recorded.</p>}{A3.secondary_typologies?.map(item => <Findings key={item.typology} current={current} title={`Secondary: ${item.typology.replaceAll('_', ' ').toLowerCase()}`} items={[item.rationale]} />)}</>}
    {A4 && <>{A4.confidence && A4.confidence_reason ? <><Findings current={current} title={`Evidence confidence: ${A4.confidence.toLowerCase()} (AI-assessed)`} items={[A4.confidence_reason]} /><p className="muted">Self-assessed evidence support, not a calibrated probability or certainty of wrongdoing.</p></> : <p className="muted">AI-assessed confidence: Not recorded.</p>}</>}
    {A2 && <details><summary>Risk explanation</summary><Findings current={current} title="Risk findings" items={A2.findings} /><Findings current={current} title="Risk counter-evidence" items={A2.counter_evidence} /><h4>Risk limitations</h4>{A2.limitations.length ? <ul>{A2.limitations.map((text, index) => <li key={index}>{text}</li>)}</ul> : <p className="muted">None recorded.</p>}</details>}
  </article>;
}
