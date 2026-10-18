import { useEffect, useRef, useState } from 'react';
import { request } from './api';

export type CorpusSummary = { cases: number; transactions: number; identities: number; reference_annotations: number;
  training_transactions: number; validation_transactions: number; initially_unprocessed: number; reference_walkthrough_cases: number;
  dataset_id?: string; scope?: string; live_triage_available?: boolean;
  advisory?: { ai_reviewed_cases: number; agent_draft_reviewed_cases: number; pending_ai_reviews: number; baseline_cases: number; reserved_cases: number } };

export function useCorpusSummary(onError: (error: unknown) => void, revision = 0) {
  const [summary, setSummary] = useState<CorpusSummary | null>(null);
  const report = useRef(onError); report.current = onError;
  useEffect(() => {
    const abort = new AbortController(); setSummary(null);
    request<CorpusSummary>('/api/demo/bulk/summary', '', {signal: abort.signal})
      .then(value => { if (!abort.signal.aborted) setSummary(value); })
      .catch(error => { if (!abort.signal.aborted) report.current(error); });
    return () => abort.abort();
  }, [revision]);
  return summary;
}
