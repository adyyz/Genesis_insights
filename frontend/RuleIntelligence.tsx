import React, { useEffect, useRef, useState } from 'react';
import { request } from './api';
import { Dialog } from './Dialog';
import { AgentTrace } from './AgentTrace';
import { Icon } from './Icon';
import { useCorpusSummary } from './useCorpusSummary';
import type { ReferenceMetrics, ReferenceSummary } from './ReferenceOverview';
import type { EvolutionDetail, Page, PrivateAlert, ReviewCounts } from './types';

type Rule = ReviewCounts & { id: string; name: string; summary?: string; category: string; condition: string; protected: boolean; detector_application_supported: boolean; alert_count: number; reviewed_fp_proportion: number | null; conclusive_reviews: number; a0_thresholds: { tp: number; fp: number }; active_detector: { id: string } | null; learning_status: string; machine_distribution: Record<string, number>; recommendations: { id: string; state: string; hypothesis: string }[] };
type BriefBody = { executive_summary: string; counts: Record<string, number>; scope: string; sample_note: string };
type Brief = { revision: number; publication: { published_at: string; body: BriefBody } | null };
type Example = { feedback: { alert_id: string; verdict: string; notes: string }; case: { id: string; customer_id: string; split: string } };
type Trend = { run_id: string; created_at: string; state: string; rules?: Record<string, { false_positive_reviews: number; conclusive_reviews: number; reviewed_alert_noise_proportion: number | null }> };
type Projection = { dataset_id: string; prepared: boolean; cases?: number; note?: string; assignment_counts?: Record<string, number>; rules?: (ReferenceMetrics & { rule_id: string })[]; fitting_results?: {rule_id: string; eligible_observations: number; status: string; candidate: {thresholds: {tp: number; fp: number}} | null}[] };
const words = (value: string) => value.replaceAll('_', ' ').toLowerCase();
const percent = (value: number | null) => value === null ? 'Unavailable' : new Intl.NumberFormat('en-AU', { style: 'percent', maximumFractionDigits: 1 }).format(value);

function OutcomeCells({ item, seeded, projected, reference }: { item: Rule; seeded: boolean; projected: boolean; reference?: ReferenceMetrics }) {
  if (seeded) {
    if (!reference) return <td colSpan={4}>Reference outcomes unavailable for this rule.</td>;
    const conclusive = reference.true_positive + reference.false_positive;
    return <><td>{item.alert_count} / {reference.cases}</td><td>{reference.true_positive} / {reference.false_positive} / {reference.inconclusive}</td><td>{percent(conclusive ? reference.false_positive / conclusive : null)}<small>n = {conclusive} conclusive {projected ? 'projections' : 'references'}</small></td><td>{reference.agreement} / {reference.comparable} comparable<small>{reference.uncertain} {projected ? 'inconclusive advisory labels' : 'uncertain heuristic labels'}</small></td></>;
  }
  return <><td>{item.alert_count} / {item.reviewed_alerts}</td><td>{item.true_positive} / {item.false_positive} / {item.inconclusive}</td><td>{percent(item.reviewed_fp_proportion)}<small>n = {item.conclusive_reviews}</small></td><td>{item.agreed} / {item.compared} comparable<small>{item.suggestion_unavailable} unavailable / {item.machine_uncertain} uncertain</small></td></>;
}

export function PublicEvolution() {
  const [rules, setRules] = useState<Rule[]>([]), [brief, setBrief] = useState<Brief | null>(null), [error, setError] = useState(false), [refresh, setRefresh] = useState(0);
  useEffect(() => {
    const controller = new AbortController(); setError(false);
    Promise.all([request<{ rules: Rule[] }>('/api/demo/bulk/evolution/rules', '', { signal: controller.signal }), request<Brief>('/api/demo/bulk/evolution/brief', '', { signal: controller.signal })])
      .then(([catalog, published]) => { if (!controller.signal.aborted) { setRules(catalog.rules); setBrief(published); } })
      .catch(() => { if (!controller.signal.aborted) setError(true); });
    return () => controller.abort();
  }, [refresh]);
  return <section className="panel"><div className="panel-heading"><h2><span aria-hidden="true">📋 </span>Published Evolution brief</h2><button className="text-button" onClick={() => setRefresh(value => value + 1)}>Refresh public brief</button></div>
    {error && <p role="status">The public brief could not be loaded. Refresh to retry.</p>}
    {brief?.publication ? <><p>{brief.publication.body.executive_summary}</p><p className="muted">{words(brief.publication.body.scope)} / published {new Date(brief.publication.published_at).toLocaleString('en-AU', { timeZone: 'UTC' })} UTC</p><dl className="review-totals">{['reviewed_alerts', 'true_positive', 'false_positive', 'inconclusive'].map(key => <div key={key}><dt>{words(key)}</dt><dd>{brief.publication!.body.counts[key]}</dd></div>)}</dl><p className="muted">{brief.publication.body.sample_note}</p></> : <p>No operator-reviewed aggregate brief has been published. The rule catalog below describes the baseline; private reports require operator sign-in.</p>}
    <RuleCatalog rules={rules} />
  </section>;
}

function RuleCatalog({ rules }: { rules: Rule[] }) {
  return <div className="table-scroll"><table className="rule-catalog"><caption>Nine baseline detector rules / no individual review outcomes</caption><thead><tr><th>Rule</th><th>Category</th><th>Trigger condition</th><th>Guard</th></tr></thead><tbody>{rules.map(rule => <tr key={rule.id}><th scope="row">{rule.name}<small className="rule-summary">{rule.summary}</small></th><td><span className="category-chip">{rule.category}</span></td><td>{rule.condition}</td><td>{rule.protected ? 'Protected / human review' : 'Human verdict required'}</td></tr>)}</tbody></table></div>;
}

type Props = { apiBase?: string; renderLayout?: (parts: { report: React.ReactNode; matrix: React.ReactNode; publication: React.ReactNode }) => React.ReactNode; discardVersion: number; version: number; token: string; detail: EvolutionDetail | null; selectedRule: string; onSelectRule: (value: string) => void; onOpenCase: (caseId: string, alertId: string, rule: string) => void; onDirty: (value: boolean) => void; navigate: (action: () => void) => void; onError: (error: unknown) => void };
export function RuleIntelligence({ apiBase = '/api/operator/bulk/evolution', renderLayout, discardVersion, token, detail, selectedRule, onSelectRule, onOpenCase, onDirty, navigate, onError, version }: Props) {
  const fieldPrefix = 'corpus-';
  const [rules, setRules] = useState<Rule[]>([]), [view, setView] = useState('Outcomes'), [category, setCategory] = useState(''), [learning, setLearning] = useState('');
  const [refresh, setRefresh] = useState(0), [loading, setLoading] = useState(true), [failed, setFailed] = useState(false);
  const summary = useCorpusSummary(onError, version + refresh);
  const [outcomeSource, setOutcomeSource] = useState('projection');
  const [references, setReferences] = useState<ReferenceSummary | null>(null), [referenceError, setReferenceError] = useState(false);
  const [projections, setProjections] = useState<Projection | null>(null);
  const activeBaseline = summary?.scope === 'ACTIVE_BASELINE';
  const seeded = activeBaseline && outcomeSource !== 'human';
  const projected = activeBaseline && outcomeSource === 'projection';
  const outcomes = projected ? projections?.prepared ? projections : null : references;
  useEffect(() => {
    const controller = new AbortController(); setReferences(null); setProjections(null); setReferenceError(false);
    if (activeBaseline) Promise.all([
      request<ReferenceSummary>('/api/demo/bulk/reference-summary', '', {signal: controller.signal}),
      request<Projection>('/api/operator/bulk/projected-reviews', token, {signal: controller.signal})
    ]).then(([value, projection]) => {
        if (controller.signal.aborted) return;
        if (value.dataset_id !== summary?.dataset_id || projection.dataset_id !== summary?.dataset_id) throw new Error('Baseline changed; refresh rule evidence before comparing outcomes.');
        setReferences(value); setProjections(projection);
        if (!projection.prepared) setOutcomeSource(previous => previous === 'projection' ? 'reference' : previous);
      }).catch(error => { if (!controller.signal.aborted) { setReferenceError(true); onError(error); } });
    return () => controller.abort();
  }, [activeBaseline, summary?.dataset_id, refresh, version, token]);
  const [examples, setExamples] = useState<Page<Example>>({ items: [], next_cursor: null }), [example, setExample] = useState<{ row: Example; detail: PrivateAlert } | null>(null), [trends, setTrends] = useState<Trend[]>([]);
  const [brief, setBrief] = useState<Brief>({ revision: 0, publication: null }), [draft, setDraft] = useState(''), [publishing, setPublishing] = useState(false), [preview, setPreview] = useState<BriefBody | null>(null), [withdrawing, setWithdrawing] = useState(false), [withdrawNotes, setWithdrawNotes] = useState(''), [message, setMessage] = useState('');
  const result = detail?.result, row = rules.find(item => item.id === selectedRule);
  const evidenceRequest = useRef<AbortController | null>(null);
  const publicationTrigger = useRef<HTMLElement | null>(null);
  useEffect(() => { setDraft(''); setWithdrawNotes(''); }, [discardVersion]);
  useEffect(() => { onDirty(!!draft || !!withdrawNotes); }, [draft, withdrawNotes, onDirty]);
  useEffect(() => {
    const controller = new AbortController(); setLoading(true); setFailed(false);
    Promise.all([request<{ rules: Rule[] }>(apiBase + '/rule-matrix', token, { signal: controller.signal }), request<Brief>(apiBase + '/public-brief', token, { signal: controller.signal }), request<Page<Trend>>(apiBase + '/trends?limit=12', token, { signal: controller.signal })])
      .then(([matrix, published, history]) => { if (!controller.signal.aborted) { setRules(matrix.rules); setBrief(published); setTrends(history.items); } })
      .catch(error => { if (!controller.signal.aborted) { setFailed(true); onError(error); } }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [token, refresh, version, apiBase]);
  useEffect(() => {
    const controller = new AbortController(); setExamples({ items: [], next_cursor: null }); setExample(null);
    if (!selectedRule) return () => { controller.abort(); evidenceRequest.current?.abort(); };
    request<Page<Example>>(`${apiBase}/rules/${selectedRule}/examples`, token, { signal: controller.signal }).then(value => { if (!controller.signal.aborted) setExamples(value); }).catch(error => { if (!controller.signal.aborted) onError(error); });
    return () => { controller.abort(); evidenceRequest.current?.abort(); };
  }, [selectedRule, token, refresh, apiBase]);
  async function inspect(item: Example) {
    evidenceRequest.current?.abort(); const controller = new AbortController(); evidenceRequest.current = controller;
    try { const value = await request<PrivateAlert>(`/api/operator/alerts/${item.feedback.alert_id}`, token, { signal: controller.signal }); if (!controller.signal.aborted) setExample({ row: item, detail: value }); }
    catch (error) { if (!controller.signal.aborted) onError(error); }
  }
  async function moreExamples() {
    if (!examples.next_cursor) return;
    evidenceRequest.current?.abort(); const controller = new AbortController(); evidenceRequest.current = controller;
    try { const value = await request<Page<Example>>(`${apiBase}/rules/${selectedRule}/examples?cursor=${encodeURIComponent(examples.next_cursor)}`, token, { signal: controller.signal }); if (!controller.signal.aborted) setExamples(previous => ({ ...value, items: [...previous.items, ...value.items] })); }
    catch (error) { if (!controller.signal.aborted) onError(error); }
  }
  async function publication(action: 'preview' | 'publish' | 'withdraw') {
    if (publishing || (action !== 'withdraw' && !detail)) return;
    if (action === 'preview') publicationTrigger.current = document.activeElement as HTMLElement | null;
    setPublishing(true); setMessage('');
    try {
      if (action === 'preview') {
        const value = await request<{ body: BriefBody }>(apiBase + '/public-brief/preview', token, { method: 'POST', body: JSON.stringify({ expected_revision: brief.revision, report_id: detail!.run.id, executive_summary: draft }) }); setPreview(value.body);
      } else {
        await request(apiBase + '/public-brief' + (action === 'withdraw' ? '/withdraw' : ''), token, { method: 'POST', body: JSON.stringify(action === 'withdraw' ? { expected_revision: brief.revision, notes: withdrawNotes } : { expected_revision: brief.revision, report_id: detail!.run.id, executive_summary: draft }) });
        setPreview(null); setWithdrawing(false); setDraft(''); setWithdrawNotes(''); setRefresh(value => value + 1); setMessage(action === 'withdraw' ? 'Public brief withdrawn.' : 'Reviewed aggregate brief published. Individual evidence remains private.');
      }
    } catch (error) { setMessage('The brief could not be updated. Your draft is preserved; refresh after a conflict.'); onError(error); }
    finally { setPublishing(false); }
  }
  const visible = rules.filter(item => (!category || item.category === category) && (!learning || item.learning_status === learning));
  function filterRules(nextCategory: string, nextLearning: string) {
    navigate(() => {
      setCategory(nextCategory); setLearning(nextLearning);
      const matches = rules.filter(item => (!nextCategory || item.category === nextCategory) && (!nextLearning || item.learning_status === nextLearning));
      if (!matches.some(item => item.id === selectedRule)) onSelectRule(matches[0]?.id || '');
    });
  }
  const reportPanel = (<section className="panel intelligence-report"><span className="eyebrow">PRIVATE / FROZEN ANALYTICAL INPUTS</span><h2><span aria-hidden="true">🧬 </span>Evolution intelligence report</h2>
      {result ? <><p className="report-summary">{result.summary}</p><p className="muted">{result.conclusive_reviews} conclusive reviews overall / {result.target_rule_reviews} HIGH_VALUE reviews / {words(result.status)}. Counts come from Python; hypotheses are AI suggestions.</p>
        {result.report_version === 2 ? <>{result.findings?.length ? result.findings.map((finding, index) => <article className="analysis-block" key={index}><h4>{finding.title} / {words(finding.rule_id)}</h4><p>{finding.explanation}</p><p className="muted">{finding.support_feedback_ids.length} supporting FP reviews / {finding.counter_feedback_ids.length} counter-reviews. Signals: {finding.signal_codes.map(words).join(', ') || 'Not recorded'}</p><button className="text-button" onClick={() => navigate(() => onSelectRule(finding.rule_id))}>Inspect supporting rule</button></article>) : <p>No supported recurring false-positive root cause recorded in this report.</p>}
        {result.recommendation_items?.map((item, index) => <article className="analysis-block" key={index}><h4>{item.title} <span className="badge neutral">{item.kind === 'INFORMATIONAL' ? 'P2 / informational / no Apply action' : 'Evidence-gated detector proposal'}</span></h4><p>{item.rationale}</p><p className="muted">{words(item.rule_id)} / {item.support_feedback_ids.length} supporting reviews</p></article>)}</> : <p className="muted">Historical report: structured cross-rule findings were not recorded. The original result is preserved.</p>}
        <ul>{result.limitations.map((text, index) => <li key={index}>{text}</li>)}</ul>
      </> : <p>{renderLayout ? 'Select a stored A5 report above.' : 'Select a stored Evolution analysis below.'} No model report or improvement is assumed.</p>}
      {detail && <AgentTrace detail={detail} />}
    </section>);
  const matrixPanel = (<section className="panel rule-matrix"><div className="panel-heading"><h2><span aria-hidden="true">📋 </span>Rule matrix</h2><button className="text-button" disabled={loading} onClick={() => setRefresh(value => value + 1)}>Refresh rule matrix</button></div><p className="muted">Current corpus: {summary?.initially_unprocessed.toLocaleString() ?? 'Loading'} {summary?.scope === 'ACTIVE_BASELINE' ? 'reserved' : 'operational'} cases within {summary?.cases.toLocaleString() ?? 'Loading'} total cases. {summary?.scope === 'ACTIVE_BASELINE' ? 'Alert counts cover the active baseline. Choose an outcome source to keep generated labels separate from genuine human reviews.' : 'Synthetic training and reserved validation are excluded.'} Reviewed-alert FP proportion is not population FPR. Missing context or any protected sanctions, post-death or structuring signal forces A0 uncertainty.</p>
      <div className="view-switch" role="group" aria-label="Rule matrix view">{['Outcomes', 'Configuration'].map(name => <button key={name} className={view === name ? 'primary' : 'text-button'} aria-pressed={view === name} onClick={() => setView(name)}>{name}</button>)}</div>
      {activeBaseline && view === 'Outcomes' && <div className="form-field"><label htmlFor={fieldPrefix + 'outcome-source'}>Outcome source</label><select id={fieldPrefix + 'outcome-source'} value={outcomeSource} onChange={event => { const value = event.target.value; navigate(() => setOutcomeSource(value)); }}><option value="projection" disabled={!projections?.prepared}>Simulated analyst projections</option><option value="reference">Seeded reference outcomes</option><option value="human">Human analyst outcomes</option></select></div>}
      {view === 'Outcomes' && <p className="context-warning">{projected ? <>{projections?.cases?.toLocaleString() ?? 'Loading'} simulated analyst cases: {projections?.assignment_counts?.AGREE ?? 'Loading'} agree / {projections?.assignment_counts?.DISAGREE ?? 'Loading'} disagree / {projections?.assignment_counts?.UNCERTAIN ?? 'Loading'} uncertainty assignments. Source advisories: {summary?.advisory?.ai_reviewed_cases ?? 'Loading'} Gemini / {summary?.advisory?.agent_draft_reviewed_cases.toLocaleString() ?? 'Loading'} agent drafts. These are generated test labels, not human reviews or verified decisions. Final uncertainty may exceed 5% because inconclusive advisories stay inconclusive. No genuine learning gate or active policy changes.</> : seeded ? <>{references?.cases.toLocaleString() ?? 'Loading'} reference-labeled cases; reserved cases are excluded. TP/FP values below are synthetic reference verdicts, not human reviews or Gemini decisions. Multi-rule counts overlap. These outcomes do not satisfy genuine learning gates.</> : 'These counts include only genuine saved human verdicts. Reference labels and agent/AI advisories do not count as human reviews.'}</p>}
      {seeded && !outcomes && <p role="status">{referenceError ? 'Seeded outcomes could not be loaded. Refresh rule matrix to retry.' : 'Loading seeded outcomes...'}</p>}
      <div className="matrix-filters"><div className="form-field"><label htmlFor={fieldPrefix + 'matrix-category'}>Rule category</label><select id={fieldPrefix + 'matrix-category'} value={category} onChange={event => filterRules(event.target.value, learning)}><option value="">All categories</option>{[...new Set(rules.map(item => item.category))].map(name => <option key={name}>{name}</option>)}</select></div><div className="form-field"><label htmlFor={fieldPrefix + 'matrix-learning-status'}>Learning status</label><select id={fieldPrefix + 'matrix-learning-status'} value={learning} onChange={event => filterRules(category, event.target.value)}><option value="">All statuses</option>{[...new Set(rules.map(item => item.learning_status))].map(name => <option key={name} value={name}>{words(name)}</option>)}</select></div></div>
      {loading && <p role="status">Loading rule evidence…</p>}{failed && <p role="status">Rule evidence could not be loaded. Refresh to retry.</p>}
      {!loading && !failed && (view !== 'Outcomes' || !seeded || outcomes) && <div className="table-scroll"><table className="matrix-data" data-view={view}><caption>{visible.length} of 9 detector rules / {view === 'Outcomes' ? projected ? 'simulated analyst projections' : seeded ? 'seeded reference outcomes' : 'human analyst outcomes' : view.toLowerCase()}</caption><thead><tr><th>Rule</th>{view === 'Outcomes' ? <><th>{projected ? 'Alerts / simulated reviews' : seeded ? 'Alerts / reference-labeled' : 'Alerts / human-reviewed'}</th><th>{projected ? 'Simulated TP / FP / inconclusive' : seeded ? 'Reference TP / FP / inconclusive' : 'Analyst TP / FP / inconclusive'}</th><th>{projected ? 'Conclusive simulated FP share' : seeded ? 'Conclusive reference FP share' : 'Reviewed FP proportion'}</th><th>{projected ? 'Advisory / projected agreement' : seeded ? 'Heuristic / reference agreement' : 'Agreement / unavailable'}</th></> : <><th>Detector condition</th><th>Baseline / active detector</th><th>A0 TP / FP cutoffs</th><th>Proposed changes</th><th>Learning / guard</th></>}</tr></thead><tbody>{visible.map(item => <tr key={item.id} className={item.id === selectedRule ? 'selected-rule' : ''}><th scope="row"><div className="matrix-rule-label"><button className="text-button" aria-pressed={item.id === selectedRule} onClick={() => navigate(() => onSelectRule(item.id))}>{item.name}</button><span className="rule-summary">{item.summary || item.condition}</span><small className="category-chip" data-category={item.category}>{item.category}</small></div></th>{view === 'Outcomes' ? <OutcomeCells item={item} seeded={seeded} projected={projected} reference={outcomes?.rules?.find(value => value.rule_id === item.id)} /> : <><td>{item.condition}</td><td>Baseline: nine frozen rules<small>Active: {item.active_detector?.id || 'Baseline'}</small>{!item.detector_application_supported && <small>Detector changes informational only</small>}</td><td>≤ {item.a0_thresholds.tp} / ≥ {item.a0_thresholds.fp}<small>Baseline: ≤ 0.25 / ≥ 0.65</small></td><td>{item.recommendations?.length ? <details className="matrix-findings"><summary>{item.recommendations.length} stored findings</summary><div>{item.recommendations.map(proposal => <p key={proposal.id}><span className="badge neutral">{words(proposal.state)}</span>{proposal.hypothesis}</p>)}</div></details> : "None recorded"}</td><td>{item.protected ? 'Protected / forced uncertain' : words(item.learning_status)}</td></>}</tr>)}</tbody></table></div>}
      {!visible.length && !loading && !failed && <p>No rules match these filters.</p>}
      {projected && projections?.prepared && <details className="analysis-block"><summary>Simulated cutoff exploration: training only</summary><p>No independent validation or operational application. Risk weights, heuristic coefficients and genuine policies are unchanged.</p><div className="table-scroll"><table><caption>Python cutoff fitting against generated projected verdicts; not measured improvement</caption><thead><tr><th>Rule</th><th>Eligible observations</th><th>Training finding</th><th>Candidate TP / FP cutoffs</th></tr></thead><tbody>{projections.fitting_results?.map(item => <tr key={item.rule_id}><th scope="row">{words(item.rule_id)}</th><td>{item.eligible_observations}</td><td><span className="badge neutral">{words(item.status)}</span></td><td>{item.candidate ? `TP <= ${item.candidate.thresholds.tp} / FP >= ${item.candidate.thresholds.fp}` : 'Baseline retained'}</td></tr>)}</tbody></table></div></details>}

      {row && <section className="rule-detail"><h3>{row.name} / recorded investigation evidence</h3><p>{row.condition}</p><dl className="review-totals">{Object.entries(row.machine_distribution).map(([name, value]) => <div key={name} data-label={name}><dt>{words(name)}</dt><dd>{value}</dd></div>)}</dl><p className="muted">Current recorded machine assessments, including unreviewed alerts. Saved human comparisons remain pinned to what the analyst reviewed.</p>
        <h4><span aria-hidden="true">📈 </span>Stored reviewed-alert trend</h4>{trends.length ? <div className="trend-series">{[...trends].reverse().map(item => { const value = item.rules?.[row.id]; return <div key={item.run_id}><span>{new Date(item.created_at).toLocaleString('en-AU', { timeZone: 'UTC', dateStyle: 'short', timeStyle: 'short' }) + ' UTC'}</span><React.Fragment>{value?.reviewed_alert_noise_proportion == null ? <span className="trend-unavailable">No conclusive reviews in this snapshot</span> : <div className="track" role="img" aria-label={`${percent(value.reviewed_alert_noise_proportion)} false-positive share among ${value.conclusive_reviews} conclusive reviews`}><span style={{ width: `${value.reviewed_alert_noise_proportion * 100}%` }} /></div>}</React.Fragment><span>{value ? percent(value.reviewed_alert_noise_proportion) : 'Unavailable'} / {value?.conclusive_reviews ?? 0} conclusive reviews</span></div>; })}</div> : <p>No stored analyses yet.</p>}<p className="muted">Each point comes from a distinct frozen review cohort; repeated A5 attempts do not add observations. Cohorts can overlap. These are reviewed-alert FP shares, not population FPR or proof of improvement.</p>
        <h4>Supporting reviewed alerts</h4>{examples.items.length ? <ul className="supporting-examples">{examples.items.map(item => <li key={item.feedback.alert_id}><span>{words(item.feedback.verdict)} / {words(item.case.split)}</span><button className="text-button" onClick={() => void inspect(item)}>Preview supporting alert</button></li>)}</ul> : <p>No supporting reviews loaded for this rule.</p>}{examples.next_cursor && <button className="text-button" onClick={() => void moreExamples()}>Load more supporting reviews</button>}
      </section>}
    </section>);
  const publicationPanel = (<section className="panel publication-panel"><div className="panel-heading"><h2>Publish a reviewed aggregate brief</h2><span className="badge neutral">Public aggregates only</span></div><p>Public visitors see only an explicitly published summary and counts. Individual analyst notes, verdicts and case outputs remain private.</p><p className="muted">{brief.publication ? 'A public brief is published.' : 'No public brief is published.'} Public revision {brief.revision}.</p>
      <div className="form-field"><label htmlFor={fieldPrefix + 'public-executive-summary'}>Public executive summary (maximum 120 words)</label><textarea className="resize-none" id={fieldPrefix + 'public-executive-summary'} rows={4} maxLength={2000} value={draft} onChange={event => setDraft(event.target.value)} /></div><p className="muted">{draft.trim() ? draft.trim().split(/\s+/).length : 0} / 120 words. {!result || result.report_version !== 2 ? 'Select a completed versioned report before previewing.' : 'Preview the exact public text and frozen counts before confirming publication.'}</p><div className="dialog-actions"><button className="primary" disabled={publishing || !draft.trim() || draft.trim().split(/\s+/).length > 120 || !result || result.report_version !== 2} onClick={() => void publication('preview')}><Icon name="eye" />Preview public brief</button>{brief.publication && <button className="secondary" disabled={publishing} onClick={() => setWithdrawing(true)}>Withdraw public brief</button>}</div>{message && <p role="status">{message}</p>}
    </section>);
  return <>
    {renderLayout ? renderLayout({ report: reportPanel, matrix: matrixPanel, publication: publicationPanel }) : <>{reportPanel}{matrixPanel}{publicationPanel}</>}
    {preview && <Dialog wide returnFocus={publicationTrigger.current} title="Publish this public brief?" description="Only this reviewed text and these frozen aggregate counts will become public. No individual records are included." onClose={() => setPreview(null)}><p>{preview.executive_summary}</p><dl className="usage-values">{Object.entries(preview.counts).map(([key, value]) => <React.Fragment key={key}><dt>{words(key)}</dt><dd>{value}</dd></React.Fragment>)}</dl><p>{preview.sample_note}</p><div className="dialog-actions"><button className="text-button" onClick={() => setPreview(null)}>Keep editing</button><button className="primary" disabled={publishing} onClick={() => void publication('publish')}>Publish reviewed brief</button></div></Dialog>}
    {withdrawing && <Dialog title="Withdraw public brief?" description="The rule catalog remains available. The published brief will be removed from public reads; its audit history is preserved." onClose={() => setWithdrawing(false)}><div className="form-field"><label htmlFor={fieldPrefix + 'withdraw-brief-notes'}>Withdrawal reason</label><textarea className="resize-none" id={fieldPrefix + 'withdraw-brief-notes'} value={withdrawNotes} maxLength={2000} onChange={event => setWithdrawNotes(event.target.value)} /></div><button className="primary" disabled={publishing || !withdrawNotes.trim()} onClick={() => void publication('withdraw')}>Withdraw reviewed brief</button></Dialog>}
    {example && <Dialog wide title="Supporting investigation" description="Private evidence for a human-reviewed alert. Model suggestions and the saved human verdict are distinct." onClose={() => setExample(null)}><p><strong>Human verdict:</strong> {words(example.row.feedback.verdict)}</p><p>{example.row.feedback.notes}</p><p><strong>Python evidence signals:</strong> inspect A1 below. <strong>Machine label:</strong> {words(example.detail.effective_assessment?.body.predicted_verdict || example.detail.outputs.A0?.predicted_verdict || 'NOT_RECORDED')}</p><p><strong>AI typology:</strong> {words(example.detail.outputs.A3?.primary_typology || 'NOT_RECORDED')}</p><p>{example.detail.outputs.A4?.overview.text || 'No brief recorded.'}</p><AgentTrace detail={example.detail} /><button className="primary" onClick={() => { const item = example; setExample(null); onOpenCase(item.row.case.id, item.row.feedback.alert_id, selectedRule); }}>Open investigation</button></Dialog>}
  </>;
}
