// Synthetic tab-only classification; authoritative evidence/scoring is computed by Python.
export const syntheticLabel = (score: number, forced: boolean, tp: number, fp: number) => forced ? 'AUTO_UNCERTAIN' : score >= fp ? 'AUTO_FP' : score <= tp ? 'AUTO_TP' : 'AUTO_UNCERTAIN';
