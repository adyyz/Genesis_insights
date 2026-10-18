import React, { useEffect, useId, useState } from 'react';
import { request } from './api';
import type { ReviewSummary } from './types';

const words = (value: string) => value.replaceAll('_', ' ').toLowerCase();
export function ReviewOverview({ token, onError, endpoint = '/api/operator/bulk/evolution/review-summary' }: { token: string; endpoint?: string; onError: (error: unknown) => void }) {
  const breakdownId = useId();
  const [data, setData] = useState<ReviewSummary | null>(null);
  const [refresh, setRefresh] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(false);
  const [dimension, setDimension] = useState<'typology' | 'secondary_typology' | 'rule' | 'identity_type' | 'risk_bucket' | 'signal'>('typology');
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true); setError(false);
    request<ReviewSummary>(endpoint, token, { signal: controller.signal })
      .then(value => { if (!controller.signal.aborted) setData(value); })
      .catch(cause => { if (!controller.signal.aborted) { setError(true); onError(cause); } })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [token, refresh, endpoint]);
  return <section className="panel review-overview" aria-label="Review outcomes">
    <div className="panel-heading"><h2><span aria-hidden="true">📈 </span>Review outcomes</h2><button className="text-button" disabled={loading} onClick={() => setRefresh(value => value + 1)}>Refresh review outcomes</button></div>
    <p>Track identity investigations and analyst decisions. AI suggestions remain separate from the saved verdict.</p>
    {loading && <p role="status">Loading review outcomes…</p>}
    {error && <p role="status">Review outcomes could not be loaded. Refresh to try again.</p>}
    {!loading && !error && data && <>
      <dl className="review-totals"><div><dt>Total cases</dt><dd>{data.total_cases.toLocaleString()}</dd><small>{data.reserved_cases !== undefined ? `${data.reserved_cases.toLocaleString()} reserved cases` : data.operational_cases === undefined ? `${data.demo_cases} interactive demo cases` : `${data.operational_cases.toLocaleString()} operational cases`} · {data.total_identities.toLocaleString()} identities</small></div><div><dt>Reviewed cases</dt><dd>{data.reviewed_cases}</dd><small>{data.reviewed_alerts} of {data.total_alerts.toLocaleString()} alerts reviewed</small></div><div><dt>Analyst verdicts</dt><dd>{data.true_positive} TP / {data.false_positive} FP</dd><small>{data.inconclusive} inconclusive</small></div><div><dt>AI–analyst agreement</dt><dd>{data.agreement_rate === null ? 'Unavailable' : new Intl.NumberFormat('en-AU', { style: 'percent', maximumFractionDigits: 1 }).format(data.agreement_rate)}</dd><small>{data.agreed} of {data.compared} comparable conclusive reviews</small></div></dl>
      <p className="muted">{data.note} {data.suggestion_unavailable} reviewed alerts have no recorded machine pre-label; {data.machine_uncertain} have an uncertain machine pre-label.</p>
      <div className="form-field"><label htmlFor={breakdownId}>Break down reviews by</label><select id={breakdownId} value={dimension} onChange={event => setDimension(event.target.value as typeof dimension)}><option value="typology">AI typology</option><option value="rule">Detector category</option><option value="identity_type">Identity type</option><option value="secondary_typology">Secondary typology (overlapping)</option><option value="risk_bucket">Python risk-score bucket</option><option value="signal">Observed signal (overlapping)</option></select></div>
      {data.groups[dimension].length ? <div className="table-scroll"><table><caption>Saved analyst verdicts by {dimension === 'rule' ? 'detector category' : words(dimension)}</caption><thead><tr><th>Category</th><th>Reviewed alerts</th><th>True positive</th><th>False positive</th><th>Inconclusive</th><th>AI agreement</th></tr></thead><tbody>{data.groups[dimension].map(row => <tr key={row.name}><th scope="row">{words(row.name)}</th><td>{row.reviewed_alerts}</td><td>{row.true_positive}</td><td>{row.false_positive}</td><td>{row.inconclusive}</td><td>{row.compared ? `${row.agreed} / ${row.compared}` : 'Unavailable'}</td></tr>)}</tbody></table></div> : <p className="muted">No saved reviews yet. Complete an identity investigation and save analyst feedback to populate this view.</p>}
    </>}
  </section>;
}
