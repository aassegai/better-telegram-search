import type { Page } from '@playwright/test';
import type { NoiseJob } from '../src/noiseTypes';

export const noiseJobId = 'a'.repeat(32);
export function readyNoiseJob(coverage: Partial<Pick<NonNullable<NoiseJob['summary']>, 'coverage_version' | 'source_complete' | 'evidence_complete' | 'policy_applicable' | 'limited_evidence_forms' | 'new_auto_omit_enabled'>> = {}) {
  return { id: noiseJobId, state: 'ready', stale: false, active: false, created_at: 1750000000,
    error: null as string | null, progress: { phase: 'candidates', completed: 51, total: 51, elapsed_seconds: 2,
      scanned_messages: 55, whole_messages: 54, forms: 51 },
    summary: { complete: true, limits: [], scanned_messages: 55, whole_messages: 54, forms: 51,
      decisions: { keep: 49, review: 1, omit: 1 }, cpu_seconds: 1, process_rss_mib: 80,
      report_sha256: '1'.repeat(64), new_auto_omit_enabled: false, ...coverage } };
}
function form(name: string, decision = 'keep') {
  return { inherited_decision: null as 'keep' | 'omit' | null, default_decision: (decision === 'omit' ? 'omit' : 'keep') as 'keep' | 'omit', default_origin: 'analysis' as 'analysis' | 'base' | 'inherited', form: name, whole_count: 1, eligible_count: 1, days: 1, authors: 1, protections: {},
    candidate: { decision, reason: decision === 'review' ? 'uncalibrated' : decision === 'omit' ? 'approved_seed' : 'no_noise_hypothesis',
      complete: true, noise: decision === 'review' ? [{ form: 'бля', operation: 'transposition', corpus_count: 20, score: 3 }] : [],
      useful: decision === 'review' ? [{ form: 'был', operation: 'substitution', corpus_count: 30, score: 3.4 }] : [],
      unchanged: { form: name, operation: 'unchanged', corpus_count: 1, score: .69 }, margin: decision === 'review' ? -.4 : null } };
}
export async function mockNoise(page: Page, initial = false) {
  const control = { job: initial ? readyNoiseJob() : null as ReturnType<typeof readyNoiseJob> | null,
    revision: 0, overrides: {} as Record<string, string>, applied: false, autoFinish: true,
    writes: [] as { path: string; method: string; body: Record<string, unknown> | null }[],
    formsGate: null as Promise<void> | null, formsStarted: 0, statusCalls: 0,
    forms: [form('бял', 'review'), form('сука', 'omit'),
      ...Array.from({ length: 48 }, (_, index) => form(`synthetic${String.fromCharCode(97 + Math.floor(index / 26))}${String.fromCharCode(97 + index % 26)}`)), form('constructor')] };
  await page.route('**/api/chats/*/noise-analyses**', async route => {
    const request = route.request(), url = new URL(request.url());
    const suffix = url.pathname.split('/noise-analyses')[1];
    const method = request.method();
    let body: Record<string, unknown> | null = null;
    if (request.postData()) body = request.postDataJSON();
    if (method !== 'GET') control.writes.push({ path: suffix, method, body });
    const send = async (json: unknown, status = 200) => {
      try { await route.fulfill({ json, status }); } catch { /* The view may abort a held read. */ }
    };
    const metadata = () => ({ id: noiseJobId, revision: control.revision,
      overrides: control.job?.stale ? {} : control.overrides, overrides_available: !control.job?.stale,
      stale: !!control.job?.stale, state: control.job?.state, current_policy_id: control.applied ? 'c'.repeat(64) : 'base',
      applied_policy_id: control.applied ? 'c'.repeat(64) : null, applied_draft_revision: control.applied ? control.revision : null });
    if (!suffix && method === 'GET') return send({ analyses: control.job ? [control.job] : [] });
    if (!suffix && method === 'POST') {
      control.job = { ...readyNoiseJob(), state: 'running', active: true, summary: null! };
      return send(control.job, 202);
    }
    if (suffix === `/${noiseJobId}` && method === 'GET') {
      control.statusCalls++;
      if (control.job?.state === 'running' && control.autoFinish && control.statusCalls >= 2) control.job = readyNoiseJob();
      return control.job ? send(control.job) : send({ detail: 'missing' }, 404);
    }
    if (suffix.endsWith('/draft') && method === 'GET') return send(metadata());
    if (suffix.endsWith('/draft') && method === 'PATCH') {
      if (body?.revision !== control.revision) return send({ detail: 'Проект изменился. Обновите его перед сохранением.' }, 409);
      control.overrides = body.overrides as Record<string, string>; control.revision++; return send(metadata());
    }
    if (suffix.endsWith('/preview')) return send({ policy_id: 'c'.repeat(64), version: 'telegram-episodes-noise-v3', revision: control.revision,
      explicit_omit_forms: Object.values(control.overrides).filter(value => value === 'omit').length,
      keep_forms: Object.values(control.overrides).filter(value => value === 'keep').length });
    if (suffix.endsWith('/apply')) {
      control.applied = true;
      if (control.job) control.job = { ...control.job, stale: true, state: 'stale', active: false };
      return send({ policy_id: 'c'.repeat(64), already_applied: false, scheduled_segments: 1 });
    }
    if (suffix.endsWith('/forms')) {
      control.formsStarted++;
      const choice = url.searchParams.get('decision');
      const rows = control.forms.filter(row => !choice || row.candidate.decision === choice);
      const offset = Number(url.searchParams.get('offset') ?? 0);
      const result = { forms: rows.slice(offset, offset + 50), next_offset: rows.length > offset + 50 ? offset + 50 : null };
      if (control.formsGate) await control.formsGate;
      return send(result);
    }
    if (suffix.endsWith('/families')) return send({ families: [{ seed: 'бля', observed_count: 4, eligible_count: 4, omit_count: 0, review_count: 4, keep_count: 0 }], next_offset: null });
    if (suffix.endsWith('/cancel')) {
      if (control.job) control.job = { ...control.job, active: false, state: control.job.stale ? 'stale' : 'cancelled' };
      return send(control.job);
    }
    if (method === 'DELETE') { control.job = null; return route.fulfill({ status: 204 }); }
    return send({ detail: 'Unexpected noise route' }, 500);
  });
  return control;
}
export async function openNoise(page: Page, english = false) {
  await page.goto('/');
  if (english) await page.getByRole('button', { name: 'RU', exact: true }).press('End');
  await page.getByRole('button', { name: english ? /^Indexing settings / : /^Настройки индексации / }).first().click();
  const panel = page.locator('.noise-panel');
  await panel.locator(':scope > details > summary').click();
  return panel;
}
