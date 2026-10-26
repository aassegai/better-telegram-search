import { expect, test } from '@playwright/test';
import type { Page } from '@playwright/test';
import { mockSearchJobs } from './search-mocks';

const spec = { query: 'first', scope: { mode: 'all' }, author_ids: [], forwarded_from: [],
  date_from: null, date_to: null, content_type: 'all', exclude_deleted: false, exact_phrase: false,
  mode: 'words', modalities: ['text'], strategy: 'exact', page_size: 1, chunk_size: 1 };
const entry = { id: 'shared', snapshot_id: 'old', spec, favorite: false, recent: true,
  completed_at: 1750000000, cache_available: true, stale_reason: null, cached_results: 2,
  models: {}, coverage: {} };
const hit = (text: string, id = 1) => ({ chat_id: 'history-chat', chat_name: 'Synthetic', message_id: id,
  timestamp: 1750000000 + id, matched_by: ['words'], messages: [{ chat_id: 'history-chat', message_id: id,
    timestamp: 1750000000 + id, author: 'Synthetic', text, kind: 'message', action: null,
    reply_to: null, forwarded_from: null, matches_filters: true, media: [], edited_timestamp: null }] });
const result = (snapshot: string, text: string, id = 1) => ({ results: [hit(text, id)], history_id: 'shared',
  snapshot_id: snapshot, job_id: `job-${snapshot}`, effective_mode: 'words', has_more: true,
  next_offset: 1, warnings: [], limit: 1, favorite: false });
const deferred = () => { let resolve: () => void = () => {}; const promise = new Promise<void>(done => { resolve = done; }); return { promise, resolve }; };

async function saved(page: Page) {
  await page.route('**/api/search-history', route => route.fulfill({ json: { entries: [entry], next_offset: null } }));
  await page.route('**/api/search-history/shared', route => route.fulfill({ json: { entry, result: result('old', 'OLD FIRST') } }));
  await page.goto('/');
  await page.getByRole('button', { name: 'История поиска', exact: true }).click();
  await page.getByRole('dialog').getByRole('button', { name: 'Открыть поиск', exact: true }).click();
  await expect(page.locator('.result-card')).toHaveCount(1);
}

test('late metadata from an older snapshot cannot discard a completed repeat with the same history ID', async ({ page }) => {
  let count = 0;
  await mockSearchJobs(page, route => route.fulfill({ json: result(++count === 1 ? 'old' : 'new', count === 1 ? 'OLD FIRST' : 'FRESH REPEAT') }));
  const entered = deferred(), release = deferred(), delivered = deferred();
  await page.route('**/api/search-history/shared/status*', async route => {
    if (new URL(route.request().url()).searchParams.get('snapshot_id') !== 'old') return route.fulfill({ json: { ...entry, snapshot_id: 'new' } });
    entered.resolve(); await release.promise;
    await route.fulfill({ json: { ...entry, cache_available: false } }).catch(() => {});
    delivered.resolve();
  });
  try {
    await page.goto('/');
    await page.getByLabel('Поисковый запрос', { exact: true }).fill('first');
    await page.getByRole('button', { name: 'Найти', exact: true }).click();
    await expect(page.locator('.result-card')).toContainText('OLD FIRST');
    await page.evaluate(() => window.dispatchEvent(new Event('bts-search-history-changed')));
    await entered.promise;
    await page.getByRole('button', { name: 'Найти', exact: true }).click();
    await expect(page.locator('.result-card')).toContainText('FRESH REPEAT');
    release.resolve(); await delivered.promise;
    await expect(page.locator('.result-card')).toContainText('FRESH REPEAT');
  } finally { release.resolve(); }
});

test('a deleted history entry detaches the star and stops metadata polling without an error loop', async ({ page }) => {
  await page.clock.install();
  let reads = 0;
  await page.route('**/api/search-history/shared/status*', route => { reads++; return route.fulfill({ status: 404, json: { detail: 'Сохранённый поиск не найден.' } }); });
  await saved(page);
  await page.evaluate(() => window.dispatchEvent(new Event('bts-search-history-changed')));
  await expect(page.locator('.submitted-search-actions .favorite-button')).toHaveCount(0);
  await page.clock.fastForward(6500);
  expect(reads).toBe(1);
  await expect(page.locator('.error[role="alert"]')).toHaveCount(0);
});

test('cached paging rejects a page from a different snapshot of the same query', async ({ page }) => {
  await page.route('**/api/search-history/shared/status*', route => route.fulfill({ json: entry }));
  await saved(page);
  await page.route('**/api/search-history/shared?*', route => {
    expect(new URL(route.request().url()).searchParams.get('snapshot_id')).toBe('old');
    return route.fulfill({ json: { entry: { ...entry, snapshot_id: 'new' }, result: result('new', 'WRONG NEW PAGE', 2) } });
  });
  await page.getByRole('button', { name: 'Показать ещё', exact: true }).click();
  await expect(page.locator('.results .error')).toContainText('Сохранённая выдача изменилась');
  await expect(page.locator('.result-card')).toHaveCount(1);
  await expect(page.locator('.result-card')).toContainText('OLD FIRST');
  await expect(page.locator('.results')).not.toContainText('WRONG NEW PAGE');
});

test('cache invalidation closes old context and prevents an in-flight page from restoring cleared cards', async ({ page }) => {
  let available = true;
  await page.route('**/api/search-history/shared/status*', route => route.fulfill({ json: { ...entry, cache_available: available } }));
  await page.route('**/api/chats/history-chat/context/**', route => route.fulfill({ json: { messages: hit('OLD CONTEXT').messages } }));
  await saved(page);
  const entered = deferred(), release = deferred(), delivered = deferred();
  await page.route('**/api/search-history/shared?*', async route => {
    entered.resolve(); await release.promise;
    await route.fulfill({ json: { entry, result: result('old', 'LATE CLEARED PAGE', 2) } }); delivered.resolve();
  });
  try {
    await page.getByRole('button', { name: 'Показать ещё', exact: true }).click(); await entered.promise;
    await page.getByRole('button', { name: 'Открыть контекст', exact: true }).click();
    await expect(page.getByRole('dialog')).toContainText('OLD CONTEXT');
    available = false;
    await page.evaluate(() => window.dispatchEvent(new Event('bts-search-history-changed')));
    await expect(page.locator('.result-card')).toHaveCount(0);
    await expect(page.getByRole('dialog')).toHaveCount(0);
    release.resolve(); await delivered.promise;
    await expect(page.locator('.result-card')).toHaveCount(0);
  } finally { release.resolve(); }
});

test('a failed history repeat preserves all result-owned fields of the previous search', async ({ page }) => {
  let count = 0;
  await mockSearchJobs(page, route => ++count === 1 ? route.fulfill({ json: result('old', 'PREVIOUS RESULT') })
    : route.fulfill({ status: 409, json: { detail: 'Synthetic repeat failure' } }));
  const other = { ...entry, id: 'other', snapshot_id: 'other-snapshot', favorite: true, spec: { ...spec, query: 'second' } };
  await page.route('**/api/search-history', route => route.fulfill({ json: { entries: [other], next_offset: null } }));
  await page.route('**/api/search-history/other', route => route.fulfill({ json: { entry: other, result: null } }));
  await page.route('**/api/search-history/shared/status*', route => route.fulfill({ json: entry }));
  const starred: string[] = [];
  await page.route('**/api/search-history/*/favorite', route => { starred.push(new URL(route.request().url()).pathname); return route.fulfill({ status: 204 }); });
  await page.goto('/');
  await page.getByLabel('Поисковый запрос', { exact: true }).fill('first');
  await page.getByRole('button', { name: 'Найти', exact: true }).click();
  await expect(page.locator('.result-card')).toContainText('PREVIOUS RESULT');
  await page.getByRole('button', { name: 'История поиска', exact: true }).click();
  await page.getByRole('dialog').getByRole('button', { name: 'Повторить поиск', exact: true }).click();
  await expect(page.locator('.error[role="alert"]')).toContainText('Synthetic repeat failure');
  await expect(page.locator('.result-card')).toContainText('PREVIOUS RESULT');
  await expect(page.locator('.submitted-search-actions .favorite-button')).toHaveAttribute('aria-pressed', 'false');
  await page.locator('.submitted-search-actions .favorite-button').click();
  await expect.poll(() => starred).toEqual(['/api/search-history/shared/favorite']);
});
