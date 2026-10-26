import type { Page, Request } from '@playwright/test';

type Reply = { status?: number; json: Record<string, unknown> };
type SearchRoute = { request: () => Request; fulfill: (reply: Reply) => Promise<void> };

/** Mock completed search jobs while keeping request specs and result retrieval separate. */
export async function mockSearchJobs(page: Page, handler: (route: SearchRoute) => unknown) {
  let serial = 0;
  const results = new Map<string, Record<string, unknown>>();
  const status = (id: string) => ({ job_id: id, state: 'completed', phase: 'render',
    completed: 0, total: 0, unit: 'messages', elapsed_seconds: 0.1, rate: null,
    eta_seconds: null, queue_position: null, warnings: [], error: null });
  await page.route('**/api/search-tabs/*/jobs', async route => {
    const id = ++serial === 1 ? 'snapshot' : `snapshot-${serial}`;
    await handler({ request: () => route.request(), fulfill: async reply => {
      if (reply.status && reply.status >= 400) return route.fulfill(reply);
      results.set(id, { limit: 20, ...reply.json, job_id: id });
      await route.fulfill({ json: status(id) });
    } });
  });
  await page.route('**/api/search-jobs/*/result**', route => {
    const id = new URL(route.request().url()).pathname.split('/').at(-2)!;
    const result = results.get(id);
    return route.fulfill(result ? { json: result } : { status: 404, json: { detail: 'Missing mock snapshot' } });
  });
}
