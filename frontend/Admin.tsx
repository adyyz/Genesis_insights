import React, { useEffect, useState } from 'react';
import { request } from './api';
import { Dialog } from './Dialog';
import { UsageDisplay } from './Usage';
import { useCorpusSummary } from './useCorpusSummary';
import type { AuditEvent, DailyControl, OperatorUsage, Page, RunSummary } from './types';

export function Admin({ token, isAdmin, canEnable, onError, onRun }: { token: string; isAdmin: boolean; canEnable: boolean; onError: (error: unknown) => void; onRun: (id: string) => void }) {
  const [usage, setUsage] = useState<OperatorUsage | null>(null);
  const [runs, setRuns] = useState<Page<RunSummary>>({ items: [], next_cursor: null });
  const [audit, setAudit] = useState<Page<AuditEvent>>({ items: [], next_cursor: null });
  const [limit, setLimit] = useState('120');
  const [enabled, setEnabled] = useState(false);
  const [refresh, setRefresh] = useState(0);
  const summary = useCorpusSummary(onError, refresh);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [confirm, setConfirm] = useState(false);
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  const limitRef = React.useRef<HTMLInputElement>(null);
  useEffect(() => {
    const controller = new AbortController(); setLoading(true); setError('');
    Promise.all([request<OperatorUsage>('/api/operator/usage?scope=corpus', token, { signal: controller.signal }),
      request<Page<RunSummary>>('/api/operator/runs?scope=corpus', token, { signal: controller.signal }),
      request<Page<AuditEvent>>('/api/operator/audit?scope=corpus', token, { signal: controller.signal })])
      .then(([summary, recent, events]) => { if (!controller.signal.aborted) { setUsage(summary); setRuns(recent); setAudit(events); setLimit(String(summary.daily.limit)); setEnabled(summary.daily.live_enabled); } })
      .catch(error => { if (!controller.signal.aborted) { setError('Admin data could not be loaded. Refresh to try again.'); onError(error); } })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [token, refresh]);
  function submit(event: React.FormEvent) {
    event.preventDefault(); setError(''); setMessage('');
    if (!usage || saving) return;
    if (!/^\d+$/.test(limit) || Number(limit) < Math.max(1, usage.daily.attempts_reserved) || Number(limit) > 120) {
      setError(`Enter a whole-number limit from ${Math.max(1, usage.daily.attempts_reserved)} to 120.`); limitRef.current?.focus(); return;
    }
    setConfirm(true);
  }
  async function save() {
    if (!usage || saving) return;
    setConfirm(false); setSaving(true); setError('');
    try {
      const daily = await request<DailyControl>('/api/admin/usage-daily', token, { method: 'PUT', body: JSON.stringify({ expected_revision: usage.daily.revision, live_enabled: enabled, limit: Number(limit) }) });
      setUsage({ ...usage, daily }); setMessage('Daily controls saved.');
    } catch (error) { setError((error as { status?: number }).status === 409 ? 'Controls changed or live AI is gated. Refresh before saving again.' : 'Controls were not saved. Refresh and retry.'); onError(error); }
    finally { setSaving(false); }
  }
  async function more(kind: 'runs' | 'audit') {
    const page = kind === 'runs' ? runs : audit;
    if (!page.next_cursor || loading) return;
    setLoading(true);
    try {
      const next = await request<Page<RunSummary> & Page<AuditEvent>>(`/api/operator/${kind}?scope=corpus&cursor=${encodeURIComponent(page.next_cursor)}`, token);
      if (kind === 'runs') setRuns(previous => ({ ...next, items: [...previous.items, ...next.items] }));
      else setAudit(previous => ({ ...next, items: [...previous.items, ...next.items] }));
    } catch (error) { onError(error); } finally { setLoading(false); }
  }
  return <><section className="panel"><div className="panel-heading"><h2>Operational usage</h2><button className="text-button" disabled={loading || saving} onClick={() => setRefresh(value => value + 1)}>Refresh admin data</button></div>
    <p className="muted">Usage and histories cover the current {summary?.cases.toLocaleString() ?? 'Loading'}-case corpus. Daily call controls apply to all attempts.</p>
    {loading && <p role="status">Loading operational data…</p>}{error && <p className="error" role="alert">{error}</p>}{message && <p role="status">{message}</p>}
    {usage && <><UsageDisplay usage={usage} /><dl className="usage-values"><dt>Provider attempts</dt><dd>{usage.provider_attempts}</dd><dt>Failed steps</dt><dd>{usage.failed_attempts}</dd><dt>Retries</dt><dd>{usage.retry_count}</dd><dt>Mean step latency</dt><dd>{usage.mean_duration_ms === null ? 'Unavailable' : `${Math.round(usage.mean_duration_ms)} ms`}</dd><dt>Daily attempt reservations</dt><dd>{usage.daily.attempts_reserved} / {usage.daily.limit}</dd><dt>UTC control day</dt><dd>{usage.daily.day_utc}</dd><dt>Live AI</dt><dd>{usage.daily.live_enabled ? 'Enabled' : 'Disabled'}</dd></dl>
      {isAdmin ? <form noValidate onSubmit={submit}><h3>Daily AI controls</h3><p>Changes apply to the next attempt. A new UTC day starts disabled.</p>
        <div className="form-field"><label htmlFor="daily-limit">Daily attempt limit</label><input id="daily-limit" ref={limitRef} type="number" min={Math.max(1, usage.daily.attempts_reserved)} max={120} step={1} value={limit} disabled={saving} onChange={event => setLimit(event.target.value)} aria-invalid={!!error} aria-describedby="daily-control-help" /></div>
        <label className="checkbox-field"><input type="checkbox" checked={enabled} disabled={saving || (!canEnable && !enabled)} onChange={event => setEnabled(event.target.checked)} /> Enable live AI today</label><p className="muted" id="daily-control-help">{canEnable ? 'Credit, kill-switch and cap gates are rechecked before each attempt.' : 'Enabling requires unexpired verified local free-tier access or cloud credit coverage.'}</p>
        <button className="primary" disabled={saving || (Number(limit) === usage.daily.limit && enabled === usage.daily.live_enabled)} aria-busy={saving}>Save daily controls</button>
      </form> : <p className="muted">You have operator read access. Daily controls require an admin account.</p>}
    </>}
  </section><section className="panel"><h2>Recent runs</h2><div className="table-scroll"><table><thead><tr><th>Run</th><th>State</th><th>Latest error</th></tr></thead><tbody>{runs.items.map(run => <tr key={run.id}><td><button className="text-button" onClick={() => onRun(run.parent_run_id || run.id)}>{run.id.slice(0, 12)}</button></td><td>{run.state}</td><td>{run.error_code || 'None'}</td></tr>)}</tbody></table></div>{!runs.items.length && !loading && <p>No runs recorded.</p>}{runs.next_cursor && <button className="text-button" disabled={loading} onClick={() => void more('runs')}>Load more runs</button>}</section>
    <section className="panel"><h2>Audit history</h2><div className="table-scroll"><table><thead><tr><th>When (UTC)</th><th>Action</th><th>Object</th></tr></thead><tbody>{audit.items.map(event => <tr key={event.id}><td>{new Date(event.occurred_at).toLocaleString('en-AU', { timeZone: 'UTC' })}</td><td>{event.action}</td><td>{event.object_collection || 'Unknown'} / {(event.object_id || event.id).slice(0, 12)}</td></tr>)}</tbody></table></div>{!audit.items.length && !loading && <p>No audit events recorded.</p>}{audit.next_cursor && <button className="text-button" disabled={loading} onClick={() => void more('audit')}>Load more audit events</button>}</section>
    {confirm && <Dialog title="Save daily AI controls?" description={`For ${usage?.daily.day_utc} UTC, live AI will be ${enabled ? 'enabled' : 'disabled'} with a limit of ${limit} attempts. Existing in-flight calls are unaffected.`} onClose={() => setConfirm(false)}><div className="dialog-actions"><button className="text-button" onClick={() => setConfirm(false)}>Keep current controls</button><button className="primary" onClick={() => void save()}>Save daily controls</button></div></Dialog>}
  </>;
}
