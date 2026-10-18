import React, { useEffect, useState } from 'react';
import { request } from './api';

export type ReferenceMetrics = { cases: number; true_positive: number; false_positive: number; inconclusive: number; comparable: number; agreement: number; uncertain: number; tp_cutoff: number; fp_cutoff: number };
export type ReferenceSummary = { dataset_id?: string; cases: number; population_fraction: number; cohort_hash: string; balance_note: string; metrics: ReferenceMetrics; rules: (ReferenceMetrics & { rule_id: string; balancing_cases: number })[]; identity_types: Record<string, number>; typologies: Record<string, number> };
export function ReferenceOverview({ onError }: { onError: (error: unknown) => void }) {
  const [failed, setFailed] = useState(false), [refresh, setRefresh] = useState(0);
  const [data, setData] = useState<ReferenceSummary | null>(null);
  useEffect(() => { const abort = new AbortController(); setFailed(false); request<ReferenceSummary>('/api/demo/bulk/reference-summary', '', { signal: abort.signal }).then(value => { if (!abort.signal.aborted) setData(value); }).catch(e => { if (!abort.signal.aborted) { setFailed(true); onError(e); } }); return () => abort.abort(); }, [refresh]);
  return <section className="panel reference-overview" aria-label="Preloaded synthetic reference evaluation">
    <div className="panel-heading"><h2><span aria-hidden="true">🧪 </span>{data ? `${data.cases.toLocaleString()}-case reference evaluation` : 'Loading reference evaluation'}</h2><span className="badge neutral">Simulated · not human reviewed</span></div>
    <p>{data ? `${(100 * data.population_fraction).toFixed(0)}% of the corpus` : 'Loading corpus coverage'} has a precomputed synthetic walkthrough: source checks, heuristic pre-labels and reference verdicts. Zero Gemini calls or human reviews are claimed. These records never satisfy genuine learning gates.</p>
    {!data ? <p role="status">Loading reference outcomes…</p> : <><p><strong>{data.metrics.cases.toLocaleString()} evaluated cases</strong> · {Object.entries(data.identity_types).map(([name, count]) => `${count.toLocaleString()} ${name.toLowerCase()}`).join(' / ')}. {data.balance_note}</p>
      <div className="table-scroll"><table><caption>Synthetic reference outcomes by rule. Multi-rule cases overlap; agreement excludes inconclusive references and uncertain predictions.</caption><thead><tr><th>Rule</th><th>Cases</th><th>Reference TP / FP / inconclusive</th><th>Heuristic agreement</th><th>Uncertain</th><th>TP ≤ / FP ≥</th></tr></thead><tbody>{data.rules.map(row => <tr key={row.rule_id}><th scope="row">{row.rule_id.toLowerCase().replaceAll('_', ' ')}</th><td>{row.cases.toLocaleString()}</td><td>{row.true_positive} / {row.false_positive} / {row.inconclusive}</td><td>{row.comparable ? `${(100 * row.agreement / row.comparable).toFixed(1)}% (${row.agreement}/${row.comparable})` : 'Unavailable'}</td><td>{row.uncertain}</td><td>{row.tp_cutoff} / {row.fp_cutoff}</td></tr>)}</tbody></table></div>
      <p className="muted">Baseline heuristic cutoffs remain unchanged. These reference outcomes demonstrate evaluation; they do not prove model accuracy or a detector improvement.</p></>}
  </section>;
}
