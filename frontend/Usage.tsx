import React, { useEffect, useState } from 'react';
import { request } from './api';
import { Dialog } from './Dialog';
import type { RunDetail, UsageSummary } from './types';

const labels: Record<string, string> = { uncached_prompt_tokens: 'Uncached prompt', cached_prompt_tokens: 'Cached prompt',
  prompt_tokens: 'Prompt (includes cached)', candidate_tokens: 'Candidate output', thought_tokens: 'Thought / reasoning', tool_prompt_tokens: 'Tool prompt', total_tokens: 'Provider total' };
const usd = (micros: number) => new Intl.NumberFormat('en-AU', { style: 'currency', currency: 'USD', maximumFractionDigits: 6 }).format(micros / 1_000_000);
const optionalCategories = new Set(['cached_prompt_tokens', 'thought_tokens', 'tool_prompt_tokens']);
export const TOAST_DISMISS_MS = 8_000;
const showCategory = (key: string, value: unknown) => !optionalCategories.has(key) || (typeof value === 'number' && value > 0);
export function usageCaption(usage: UsageSummary) {
  if (usage.provider_attempts === 0) return 'No model attempt · tokens and cost not applicable';
  return `${usage.total_tokens === null ? 'Tokens unavailable' : usage.total_tokens.toLocaleString('en-AU') + ' provider tokens'} · ${usd(usage.known_estimated_usd_micros)} known estimated USD · ${usage.unknown_attempts} unknown attempts`;
}

export function UsageDisplay({ usage }: { usage: UsageSummary }) {
  const categories = usage.categories ? Object.entries(usage.categories) : [];
  const total = categories.reduce((sum, [, value]) => sum + value, 0);
  const showChart = total > 0 && usage.total_tokens === total;
  let offset = 0;
  return <div className={'usage-display' + (showChart ? '' : ' usage-without-chart')}>
    {showChart ? <div className="usage-chart"><svg viewBox="0 0 100 100" role="img" aria-label={`${total} provider tokens by category`}>
      {categories.map(([key, value], index) => { const start = offset; offset += value / total * 100; return <circle key={key} className={`usage-segment usage-color-${index}`} cx="50" cy="50" r="38" fill="none" strokeWidth="13" pathLength="100" strokeDasharray={`${value / total * 100} ${100 - value / total * 100}`} strokeDashoffset={-start} transform="rotate(-90 50 50)" />; })}
      <text x="50" y="49" textAnchor="middle">{total.toLocaleString('en-AU')}</text><text className="usage-chart-caption" x="50" y="60" textAnchor="middle">total tokens</text>
    </svg></div> : <div className="usage-summary"><span className="eyebrow">AI token usage</span><strong>{usage.provider_attempts === 0 ? 'No AI usage yet' : usage.total_tokens === null ? 'Total not reported' : `${usage.total_tokens.toLocaleString('en-AU')} tokens reported`}</strong><p>{usage.provider_attempts === 0 ? 'Token counts will appear after an AI request.' : usage.total_tokens === null ? 'Some AI requests have no reported token counts. Available counts are listed below.' : 'We have a token total, but not enough detail to split it into a chart. The available counts are listed below.'}</p></div>}
    <div><dl className="usage-values">{(usage.categories ? categories : Object.entries(usage.known_categories)).filter(([key, value]) => key !== 'total_tokens' && showCategory(key, value)).map(([key, value]) => <React.Fragment key={key}><dt>{labels[key] || key}</dt><dd>{value === null ? usage.provider_attempts === 0 ? 'Not applicable' : 'Unknown' : value.toLocaleString('en-AU')}</dd></React.Fragment>)}
      <dt>Provider total tokens</dt><dd>{usage.total_tokens === null ? usage.provider_attempts === 0 ? 'Not applicable' : 'Unknown' : usage.total_tokens.toLocaleString('en-AU')}</dd>
      <dt>Known estimated USD</dt><dd>{usage.provider_attempts === 0 ? 'Not applicable' : usd(usage.known_estimated_usd_micros)}</dd><dt>Unknown attempts</dt><dd>{usage.unknown_attempts}</dd><dt>Not applicable</dt><dd>{usage.not_applicable_attempts}</dd></dl>
      <p className="muted">Model cost estimates are not a cloud invoice or a remaining-credit balance.</p></div>
  </div>;
}

export function PublicUsage({ version }: { version: number }) {
  const [usage, setUsage] = useState<UsageSummary | null>(null);
  const [error, setError] = useState('');
  const [refresh, setRefresh] = useState(0);
  useEffect(() => {
    const controller = new AbortController(); setError('');
    request<UsageSummary>('/api/demo/usage', '', { signal: controller.signal }).then(value => { if (!controller.signal.aborted) setUsage(value); })
      .catch(() => { if (!controller.signal.aborted) setError('Usage is unavailable. Refresh to try again.'); });
    return () => controller.abort();
  }, [version, refresh]);
  return <section className="panel public-usage"><div className="panel-heading"><h3>Usage</h3><button className="text-button" onClick={() => setRefresh(value => value + 1)}>Refresh usage</button></div>
    <p className="muted">Measured model usage across this synthetic demonstration. Available without signing in.</p>
    {error && <p role="status">{error}</p>}{usage ? <>{usage.provider_attempts === 0 && <p>No model usage has been recorded.</p>}<UsageDisplay usage={usage} /></> : !error && <p role="status">Loading usage…</p>}
  </section>;
}

export function RunUsageDialog({ runId, token, onClose, onError }: { runId: string; token: string; onClose: () => void; onError: (error: unknown) => void }) {
  const [run, setRun] = useState<RunDetail | null>(null);
  const [error, setError] = useState('');
  const [refresh, setRefresh] = useState(0);
  useEffect(() => {
    const controller = new AbortController(); setRun(null); setError('');
    request<RunDetail>(`/api/operator/runs/${runId}`, token, { signal: controller.signal }).then(value => { if (!controller.signal.aborted) setRun(value); })
      .catch(error => { if (!controller.signal.aborted) { setError('Run details could not be loaded.'); onError(error); } });
    return () => controller.abort();
  }, [runId, token, refresh]);
  return <Dialog wide title="Run usage" description="Private measured usage for this parent run, including every child, leg and retry." onClose={onClose}>
    {error && <p role="alert">{error}</p>}<button className="text-button" onClick={() => setRefresh(value => value + 1)}>Refresh run details</button>
    {run ? <><p>Parent: {run.state} · {run.children.length} alert runs · {run.attempts.filter(attempt => attempt.attempt_number > 1).length} retries</p><UsageDisplay usage={run.usage} />
      <div className="run-attempts">{run.attempts.map(attempt => <article key={attempt.id}><h3>{attempt.leg_id} · attempt {attempt.attempt_number}</h3><p>{attempt.state} {attempt.error_code && `· ${attempt.error_code}`}</p>
        <dl className="usage-values"><dt>Model</dt><dd>{attempt.model_id || 'Not applicable'}</dd><dt>Duration</dt><dd>{attempt.duration_ms === null ? 'Unknown' : `${attempt.duration_ms} ms`}</dd><dt>Token pre-count</dt><dd>{attempt.context_precount_tokens ?? 'Not applicable'}</dd><dt>Validation</dt><dd>{attempt.validation.provider_rejection ? attempt.validation.provider_rejection === 'SCHEMA_CONFIGURATION' ? 'Provider rejected the output schema' : 'Provider rejected the request' : attempt.validation.schema_pass ? 'Schema passed' : attempt.validation.missing_fields?.length ? 'Missing required fields: ' + attempt.validation.missing_fields.join(', ') : attempt.validation.error_types?.includes('missing') ? 'Required field missing; name not recorded' : attempt.validation.schema_pass === false ? 'Schema rejected' : 'Not validated'}</dd><dt>Rate card</dt><dd>{attempt.rate_card_id || 'Unavailable'}</dd><dt>Estimated USD</dt><dd>{attempt.cost_status === 'NOT_APPLICABLE' ? 'Not applicable' : attempt.estimated_usd_micros === null ? 'Unknown' : usd(attempt.estimated_usd_micros)}</dd>
          {Object.entries(labels).filter(([key]) => key !== 'uncached_prompt_tokens' && showCategory(key, attempt[key as keyof typeof attempt])).map(([key, label]) => <React.Fragment key={key}><dt>{label}</dt><dd>{attempt.usage_status === 'NOT_APPLICABLE' ? 'Not applicable' : attempt[key as keyof typeof attempt] === null ? 'Unknown' : String(attempt[key as keyof typeof attempt] ?? 'Unknown')}</dd></React.Fragment>)}
        </dl></article>)}</div></> : !error && <p role="status">Loading run details…</p>}
  </Dialog>;
}

export function CompletionToast({ run, onOpen, onDismiss }: { run: RunDetail; onOpen: () => void; onDismiss: () => void }) {
  return <aside className="completion-toast" aria-label="Run completion" style={{ '--toast-duration': `${TOAST_DISMISS_MS}ms` } as React.CSSProperties}><div role="status"><strong>Investigation batch {run.state.toLowerCase()}</strong><p>{usageCaption(run.usage)}</p></div><div className="dialog-actions"><button className="text-button" onClick={onOpen}>View usage</button><button className="text-button" onClick={onDismiss}>Dismiss notification</button></div><div className="toast-countdown" aria-hidden="true"><span /></div><small className="toast-dismiss-note">Closes automatically after {TOAST_DISMISS_MS / 1000} seconds</small></aside>;
}
