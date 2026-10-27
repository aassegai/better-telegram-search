export type NoiseDecision = 'keep' | 'omit';
export type NoiseJob = {
  id: string; state: string; stale: boolean; active?: boolean; created_at: number;
  error?: string | null;
  progress: { phase: string; completed: number; total: number | null; elapsed_seconds: number;
    scanned_messages?: number; whole_messages?: number; forms?: number; waiting_reason?: string | null };
  summary?: { complete: boolean; limits: string[]; scanned_messages: number; whole_messages: number;
    coverage_version?: string; source_complete?: boolean; evidence_complete?: boolean;
    policy_applicable?: boolean; limited_evidence_forms?: number; new_auto_omit_enabled?: boolean;
    forms: number; decisions: Record<string, number>; cpu_seconds: number; process_rss_mib: number; report_sha256?: string } | null;
};
export type NoiseDraft = { id: string; revision: number; overrides: Record<string, NoiseDecision>;
  overrides_available: boolean; stale: boolean; current_policy_id: string; applied_policy_id: string | null;
  applied_draft_revision: number | null };
export type NoisePreview = { policy_id: string; version: string; revision: number; explicit_omit_forms: number; keep_forms: number };
export type NoiseHypothesis = { form: string; operation: string; corpus_count: number; score: number };
export type NoiseForm = { form: string; whole_count: number; eligible_count: number; days: number;
  inherited_decision?: NoiseDecision | null; default_decision?: NoiseDecision; default_origin?: 'inherited' | 'base' | 'analysis';
  authors: number; protections: Record<string, number>; candidate: null | { decision: string; reason: string;
    complete: boolean; noise: NoiseHypothesis[]; useful: NoiseHypothesis[]; unchanged: NoiseHypothesis;
    margin: number | null } };
export type NoiseFamily = { seed: string; observed_count: number; eligible_count: number;
  omit_count: number; review_count: number; keep_count: number };
export type NoisePage = { forms?: NoiseForm[]; families?: NoiseFamily[]; next_offset: number | null };
export const noiseActive = (job: NoiseJob | null) => job?.active ?? ['queued', 'running', 'cancelling'].includes(job?.state ?? '');
export const noiseLabel = (labels: Record<string, string>, key: string | undefined, fallback: string) => key !== undefined && Object.hasOwn(labels, key) ? labels[key] : fallback;
export function noiseApplicable(summary: NoiseJob['summary']) {
  if (!summary || !Array.isArray(summary.limits) || summary.new_auto_omit_enabled !== false) return false;
  if (summary.coverage_version === undefined) return summary.complete === true && summary.limits.length === 0
    && !['source_complete', 'evidence_complete', 'policy_applicable', 'limited_evidence_forms'].some(key => Object.hasOwn(summary, key));
  const complete = summary.limits.length === 0;
  return summary.coverage_version === 'source-and-evidence-v1'
    && summary.source_complete === true && summary.policy_applicable === true
    && summary.new_auto_omit_enabled === false && summary.evidence_complete === complete
    && summary.complete === complete && new Set(summary.limits).size === summary.limits.length
    && summary.limits.every(limit => limit === 'candidate_limit' || limit === 'payload_limit');
}
