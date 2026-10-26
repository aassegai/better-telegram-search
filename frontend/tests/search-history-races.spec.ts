import { expect, test } from './fixtures';
import type { Page } from '@playwright/test';

const deferred = () => { let resolve: () => void = () => {}; const promise = new Promise<void>(done => { resolve = done; }); return { promise, resolve }; };

async function saved(page: Page) {
  const token = (await (await page.request.get('/api/session')).json()).token;
  const headers = { 'X-Session-Token': token };
  await page.request.delete('/api/search-history/collections/favorites', { headers });
  await page.request.delete('/api/search-history/collections/history', { headers });
  const response = await page.request.post('/api/search-tabs', { headers, data: {
    draft: { query: 'велосипед', mode: 'words', modalities: ['text'], page_size: 1, chunk_size: 1 },
  } });
  const tab = await response.json();
  await page.goto('/');
  await page.getByRole('button', { name: 'Найти', exact: true }).click();
  await expect(page.locator('.result-card')).toHaveCount(1);
  await expect(page.getByRole('button', { name: 'Найти', exact: true })).toBeEnabled();
  const metadata = (await (await page.request.get(`/api/search-tabs/${tab.id}`)).json());
  return { tab: metadata, headers };
}

test('late metadata from an older snapshot cannot discard a completed repeat with the same history ID', async ({ page }) => {
  const { tab } = await saved(page);
  const entered = deferred(), release = deferred(), delivered = deferred();
  let held = false;
  await page.route(`**/api/search-tabs/${tab.id}`, async route => {
    if (route.request().method() !== 'GET' || held) return route.continue();
    held = true;
    const value = await (await route.fetch()).json();
    entered.resolve(); await release.promise;
    await route.fulfill({ json: { ...value, snapshot: { ...value.snapshot, cache_available: false } } }).catch(() => {});
    delivered.resolve();
  });
  try {
    await page.evaluate(() => window.dispatchEvent(new Event('bts-search-history-changed')));
    await entered.promise;
    await page.getByRole('button', { name: 'Найти', exact: true }).click();
    await expect.poll(async () => (await (await page.request.get(`/api/search-tabs/${tab.id}`)).json()).snapshot.snapshot_id).not.toBe(tab.snapshot.snapshot_id);
    await expect(page.getByRole('button', { name: 'Найти', exact: true })).toBeEnabled();
    release.resolve(); await delivered.promise;
    await expect(page.locator('.result-card')).toHaveCount(1);
    await expect(page.locator('.submitted-search')).toHaveText('Результаты запроса: велосипед');
  } finally { release.resolve(); }
});

test('deleting recent history retains an open tab snapshot and its star can recreate the entry', async ({ page }) => {
  const { headers } = await saved(page);
  const removed = await page.request.delete('/api/search-history/collections/history', { headers });
  expect(removed.ok()).toBeTruthy();
  await page.evaluate(() => window.dispatchEvent(new Event('bts-search-history-changed')));
  const star = page.locator('.submitted-search-actions .favorite-button');
  await expect(star).toHaveAttribute('aria-pressed', 'false');
  await expect(page.locator('.result-card')).toHaveCount(1);
  await star.click();
  await expect(star).toHaveAttribute('aria-pressed', 'true');
  const history = (await (await page.request.get('/api/search-history')).json()).entries;
  expect(history).toHaveLength(1);
  expect(history[0]).toMatchObject({ favorite: true, spec: { query: 'велосипед' } });
  await expect(page.locator('.error[role="alert"]')).toHaveCount(0);
});

test('cached paging rejects a page from a different snapshot of the same query', async ({ page }) => {
  const { tab } = await saved(page);
  await page.route(`**/api/search-tabs/${tab.id}/result?*`, async route => {
    expect(new URL(route.request().url()).searchParams.get('snapshot_id')).toBe(tab.snapshot.snapshot_id);
    const value = await (await route.fetch()).json();
    await route.fulfill({ json: { ...value, entry: { ...value.entry, snapshot_id: 'wrong-snapshot' } } });
  });
  await page.getByRole('button', { name: 'Показать ещё', exact: true }).click();
  await expect(page.locator('.results .error')).toContainText('Сохранённая выдача изменилась');
  await expect(page.locator('.result-card')).toHaveCount(1);
});

test('cache invalidation closes old context and prevents an in-flight page from restoring cleared cards', async ({ page }) => {
  const { tab, headers } = await saved(page);
  const entered = deferred(), release = deferred(), delivered = deferred();
  await page.route(`**/api/search-tabs/${tab.id}/result?*`, async route => {
    const response = await route.fetch();
    entered.resolve(); await release.promise;
    await route.fulfill({ response }); delivered.resolve();
  });
  try {
    await page.getByRole('button', { name: 'Показать ещё', exact: true }).click(); await entered.promise;
    await page.getByRole('button', { name: 'Открыть контекст', exact: true }).click();
    await expect(page.getByRole('dialog')).toBeVisible();
    const cleared = await page.request.delete('/api/search-history/collections/cache', { headers });
    expect(cleared.ok()).toBeTruthy();
    await page.evaluate(() => window.dispatchEvent(new Event('bts-search-history-changed')));
    await expect(page.locator('.result-card')).toHaveCount(0);
    await expect(page.getByRole('dialog')).toHaveCount(0);
    release.resolve(); await delivered.promise;
    await expect(page.locator('.result-card')).toHaveCount(0);
  } finally { release.resolve(); }
});

test('a failed history repeat preserves all result-owned fields and stars the previous completed search', async ({ page }) => {
  const { tab, headers } = await saved(page);
  const accepted = await page.locator('.result-card').allTextContents();
  const response = await page.request.post('/api/search-jobs', {
    headers, data: { ...tab.snapshot.spec, query: 'literal' },
  });
  const job = await response.json();
  await expect.poll(async () => (await (await page.request.get(`/api/search-jobs/${job.job_id}`)).json()).state).toBe('completed');
  await page.route('**/api/search-tabs/*/jobs', route => route.fulfill({ status: 409, json: { detail: 'Synthetic repeat failure' } }));
  await page.getByRole('button', { name: 'История поиска', exact: true }).click();
  const other = page.getByRole('dialog').locator('.saved-search').filter({ has: page.getByText('literal', { exact: true }) });
  await other.getByRole('button', { name: 'Повторить поиск', exact: true }).click();
  await expect(page.locator('.error[role="alert"]')).toContainText('Synthetic repeat failure');
  await expect(page.locator('.result-card')).toHaveText(accepted);
  await expect(page.locator('.submitted-search')).toHaveText('Результаты запроса: велосипед');
  const star = page.locator('.submitted-search-actions .favorite-button');
  await expect(star).toHaveAttribute('aria-pressed', 'false');
  await star.click();
  await expect(star).toHaveAttribute('aria-pressed', 'true');
  const history = (await (await page.request.get('/api/search-history')).json()).entries;
  expect(history.find((entry: { favorite: boolean }) => entry.favorite).spec.query).toBe('велосипед');
});
