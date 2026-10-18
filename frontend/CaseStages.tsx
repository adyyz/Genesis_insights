import React from 'react';
import { Icon } from './Icon';
import type { CaseRecord } from './types';

export function caseProgress(current?: CaseRecord | null) {
  const relevantAlerts = current?.alerts.filter(alert => alert.applicable !== false || alert.frozen || ['QUEUED', 'RUNNING'].includes(alert.status)) || [];
  return { relevantAlerts, triageDone: relevantAlerts.length > 0 && relevantAlerts.every(alert => alert.status === 'COMPLETE' || alert.frozen),
    reviewDone: relevantAlerts.length > 0 && relevantAlerts.every(alert => alert.frozen) };
}

export function CaseStages({ current, token }: { current: CaseRecord; token: string }) {
  const { relevantAlerts, triageDone, reviewDone } = caseProgress(current);
  return <ol className="workflow-strip case-workflow" aria-label="Case workflow"><li className="workflow-step complete"><span><Icon name="check" size={11} /></span><div><strong>Source evidence</strong><small>Computed</small></div></li><li className={"workflow-step " + (current.risk.overall_score === null ? "" : "complete")}><span>{current.risk.overall_score === null ? "!" : <Icon name="check" size={11} />}</span><div><strong>Risk scoring</strong><small>{current.risk.overall_score === null ? "Needs context" : "Computed"}</small></div></li><li className={'workflow-step ' + (token && triageDone ? 'complete' : '')}><span>{!token ? <Icon name="lock" size={11} /> : triageDone ? <Icon name="check" size={11} /> : '3'}</span><div><strong>AI triage</strong><small>{!token ? 'Operator-only status' : triageDone ? 'Completed' : relevantAlerts.some(a => ['QUEUED', 'RUNNING'].includes(a.status)) ? 'In progress' : relevantAlerts.length ? 'Awaiting investigation' : 'No applicable alerts'}</small></div></li><li className={'workflow-step ' + (token && reviewDone ? 'complete' : '')}><span><Icon name={token && reviewDone ? 'check' : 'lock'} size={11} /></span><div><strong>Analyst review</strong><small>{!token ? 'Operator-only status' : reviewDone ? 'Saved and frozen' : relevantAlerts.some(a => a.status === 'COMPLETE' && !a.frozen) ? 'Ready for review' : 'Requires validated triage'}</small></div></li></ol>;
}
