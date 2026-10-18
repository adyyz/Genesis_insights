import type { PrivateAlert } from './types';

const names: Record<string, string> = { A1: 'Evidence enrichment', A2: 'Risk explanation', A3: 'Categorization', A4: 'Investigation brief', A0: 'Machine pre-label', A5: 'Evolution analysis' };
export function AgentTrace({ detail }: { detail: Pick<PrivateAlert, 'agent_trace'> }) {
  const rows = detail.agent_trace || [];
  return <details className="agent-trace"><summary>Agent execution trace</summary>
    <p className="muted">{rows[0]?.leg_id === 'A5' ? 'A5 belongs to this analytical run and its frozen inputs. It runs separately after analyst feedback.' : 'Recorded stages for the latest attempt. Earlier completed output remains linked to its original investigation. A5 runs separately after analyst feedback.'}</p>
    {rows.length ? rows.map(row => <section key={row.leg_id} className="analysis-block"><h4>{row.leg_id} / {names[row.leg_id]} <span className={'badge ' + (row.state === 'SUCCEEDED' ? '' : 'neutral')}>{row.state.replaceAll('_', ' ').toLowerCase()}</span></h4>
      <p>{row.model_id || (row.provenance === 'DETERMINISTIC' ? 'Python / deterministic' : 'No model recorded')} / {row.attempts} attempts{row.error_code && ` / ${row.error_code}`}</p>
      {row.output !== null && <details><summary>Inspect recorded output</summary><pre>{JSON.stringify(row.output, null, 2)}</pre></details>}
    </section>) : <p>No agent attempt recorded for this alert.</p>}
  </details>;
}
