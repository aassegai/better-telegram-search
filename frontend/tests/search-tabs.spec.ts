import { expect, test } from './fixtures';
import type { Page } from '@playwright/test';

async function textSearch(page: Page, query: string) {
  await page.getByLabel('Поисковый запрос', { exact: true }).fill(query);
  await page.getByLabel('Режим поиска', { exact: true }).selectOption('words');
  await page.getByRole('checkbox', { name: 'Изображения', exact: true }).uncheck();
  await page.getByRole('checkbox', { name: 'OCR', exact: true }).uncheck();
  await page.getByRole('button', { name: 'Найти', exact: true }).click();
  await expect(page.locator('.submitted-search')).toHaveText(`Результаты запроса: ${query}`);
}

test('tabs restore independent drafts, filters, results and the active tab without rerunning models', async ({ page }) => {
  let searches = 0;
  await page.route('**/api/search-tabs/*/jobs', route => { searches++; return route.continue(); });
  await page.goto('/');
  await page.getByLabel('Автор', { exact: true }).selectOption('bob');
  const workshop = page.locator('.chat-item').filter({ hasText: 'мастерская' }).getByRole('checkbox');
  await workshop.uncheck();
  await textSearch(page, 'велосипед');
  await expect(page.locator('.result-card')).toHaveCount(1);
  await expect(page.locator('.result-card')).toContainText('красный велосипед');
  await page.getByLabel('Поисковый запрос', { exact: true }).fill('черновик A');
  await page.getByRole('button', { name: 'Новая вкладка поиска', exact: true }).click();
  await expect(page.getByLabel('Поисковый запрос', { exact: true })).toHaveValue('');
  await expect(workshop).toBeChecked();
  await textSearch(page, 'literal');
  await expect(page.locator('.result-card').first()).toContainText('literal');
  await page.getByRole('tab', { name: /черновик A/ }).click();
  await expect(page.getByLabel('Поисковый запрос', { exact: true })).toHaveValue('черновик A');
  await expect(page.getByLabel('Автор', { exact: true })).toHaveValue('bob');
  await expect(workshop).not.toBeChecked();
  await expect(page.locator('.submitted-search')).toHaveText('Результаты запроса: велосипед');
  await expect(page.locator('.result-card')).toHaveCount(1);
  await page.reload();
  await expect(page.getByRole('tab')).toHaveCount(2);
  await expect(page.getByRole('tab', { name: /черновик A/ })).toHaveAttribute('aria-selected', 'true');
  await expect(page.getByLabel('Поисковый запрос', { exact: true })).toHaveValue('черновик A');
  await expect(page.locator('.result-card')).toContainText('красный велосипед');
  await page.getByRole('tab', { name: /^literal/ }).click();
  await expect(page.getByLabel('Поисковый запрос', { exact: true })).toHaveValue('literal');
  await expect(page.getByLabel('Автор', { exact: true })).toHaveValue('');
  await expect(page.locator('.submitted-search')).toHaveText('Результаты запроса: literal');
  expect(searches).toBe(2);
});

test('late results finish in their own tab and closing it never cancels the neighboring search', async ({ page }) => {
  let release!: () => void;
  let firstJob = '';
  const cancels: string[] = [];
  await page.route('**/api/search-tabs/*/jobs', async route => {
    const response = await route.fetch();
    const job = await response.json();
    if (!firstJob) firstJob = job.job_id;
    await route.fulfill({ response });
  });
  await page.route('**/api/search-jobs/*/result', async route => {
    const response = await route.fetch();
    if (route.request().url().includes(firstJob)) await new Promise<void>(resolve => { release = resolve; });
    await route.fulfill({ response });
  });
  await page.route('**/api/search-jobs/*/cancel', route => {
    cancels.push(new URL(route.request().url()).pathname.split('/').at(-2)!);
    return route.continue();
  });
  await page.goto('/');
  await page.getByLabel('Поисковый запрос', { exact: true }).fill('велосипед');
  await page.getByRole('button', { name: 'Найти', exact: true }).click();
  await expect.poll(() => !!release).toBe(true);
  await page.getByRole('button', { name: 'Новая вкладка поиска', exact: true }).click();
  await textSearch(page, 'literal');
  await expect(page.locator('.result-card').first()).toContainText('literal');
  release();
  await expect(page.getByRole('tab', { name: /^велосипед/ }).locator('.tab-busy')).toHaveCount(0);
  await expect(page.locator('.submitted-search')).toHaveText('Результаты запроса: literal');
  await page.getByRole('tab', { name: /^велосипед/ }).click();
  await expect(page.locator('.submitted-search')).toHaveText('Результаты запроса: велосипед');
  await expect(page.locator('.result-card')).toHaveCount(2);
  await page.getByRole('button', { name: 'Закрыть вкладку велосипед', exact: true }).click();
  await expect(page.getByRole('tab')).toHaveCount(1);
  await expect(page.locator('.submitted-search')).toHaveText('Результаты запроса: literal');
  expect(cancels).toEqual([]);
});

test('history opens in a separate tab and keeps independently paged snapshots after history eviction', async ({ page }) => {
  const token = (await (await page.request.get('/api/session')).json()).token;
  const headers = { 'X-Session-Token': token };
  await page.request.delete('/api/search-history/collections/favorites', { headers });
  await page.request.delete('/api/search-history/collections/history', { headers });
  await page.goto('/');
  await textSearch(page, 'велосипед');
  const current = (await (await page.request.get('/api/search-tabs')).json()).tabs[0];
  await page.getByRole('button', { name: 'История поиска', exact: true }).click();
  const saved = page.getByRole('dialog').locator('.saved-search').filter({ has: page.getByText('велосипед', { exact: true }) });
  await saved.getByRole('button', { name: 'Открыть в новой вкладке', exact: true }).click();
  await expect(page.getByRole('tab')).toHaveCount(2);
  await expect(page.locator('.result-card')).toHaveCount(2);
  const list = (await (await page.request.get('/api/search-tabs')).json()).tabs;
  const other = list.find((tab: { id: string }) => tab.id !== current.id);
  for (let i = 0; i < 11; i++) {
    const accepted = await page.request.post(`/api/search-tabs/${other.id}/jobs`, {
      headers, data: { ...current.draft, query: `уникальныйтест${i}`, mode: 'words', modalities: ['text'] },
    });
    expect(accepted.status()).toBe(202);
    const job = await accepted.json();
    await expect.poll(async () => (await (await page.request.get(`/api/search-jobs/${job.job_id}`)).json()).state).toBe('completed');
  }
  const history = (await (await page.request.get('/api/search-history')).json()).entries;
  expect(history.filter((entry: { recent: boolean }) => entry.recent)).toHaveLength(10);
  expect(history.some((entry: { spec: { query: string } }) => entry.spec.query === 'велосипед')).toBe(false);
  await page.getByRole('tab').first().click();
  await expect(page.locator('.result-card')).toHaveCount(2);
  await page.locator('.submitted-search-actions .favorite-button').click();
  await expect(page.locator('.submitted-search-actions .favorite-button')).toHaveAttribute('aria-pressed', 'true');
  const updated = (await (await page.request.get('/api/search-history')).json()).entries;
  expect(updated.some((entry: { favorite: boolean; spec: { query: string } }) => entry.favorite && entry.spec.query === 'велосипед')).toBe(true);
});

test('tabs enforce their bound and keyboard navigation survives reload in English on a narrow dark layout', async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 700 });
  await page.emulateMedia({ colorScheme: 'dark' });
  await page.addInitScript(() => localStorage.setItem('bts.language', 'en'));
  await page.goto('/');
  const add = page.getByRole('button', { name: 'New search tab', exact: true });
  await page.getByLabel('Search query', { exact: true }).fill('first');
  await add.click();
  await page.getByLabel('Search query', { exact: true }).fill('second');
  await page.getByRole('tab', { selected: true }).press('Home');
  await expect(page.getByLabel('Search query', { exact: true })).toHaveValue('first');
  await expect(page.getByRole('tab', { selected: true })).toBeFocused();
  await page.keyboard.press('End');
  await expect(page.getByLabel('Search query', { exact: true })).toHaveValue('second');
  await expect(page.getByRole('tab', { selected: true })).toBeFocused();
  for (let count = 3; count <= 8; count++) {
    await add.click();
    await expect(page.getByRole('tab')).toHaveCount(count);
  }
  await expect(add).toBeDisabled();
  await page.getByRole('tab', { selected: true }).press('Delete');
  await expect(page.getByRole('tab')).toHaveCount(7);
  await expect(page.getByRole('tab', { selected: true })).toBeFocused();
  await expect(add).toBeEnabled();
  await page.reload();
  await expect(page.getByRole('tab')).toHaveCount(7);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
});

test('reload while a save response is delayed retains the latest editor draft', async ({ page }) => {
  let release!: () => void;
  let held = false;
  await page.goto('/');
  await expect(page.getByLabel('Поисковый запрос', { exact: true })).toBeVisible();
  await page.route('**/api/search-tabs/*', async route => {
    if (route.request().method() !== 'PATCH' || held) return route.continue();
    held = true;
    const response = await route.fetch();
    await new Promise<void>(resolve => { release = resolve; });
    await route.fulfill({ response }).catch(() => {});
  });
  try {
    await page.getByLabel('Поисковый запрос', { exact: true }).fill('already committed');
    await expect.poll(() => !!release).toBe(true);
    await page.getByLabel('Поисковый запрос', { exact: true }).fill('latest pending draft');
    await page.reload();
    await expect(page.getByLabel('Поисковый запрос', { exact: true })).toHaveValue('latest pending draft');
  } finally { release?.(); }
});

test('a tab closed by another window is reconciled and does not trap navigation', async ({ page }) => {
  await page.goto('/');
  await page.getByLabel('Поисковый запрос', { exact: true }).fill('first');
  await page.getByRole('button', { name: 'Новая вкладка поиска', exact: true }).click();
  await page.getByLabel('Поисковый запрос', { exact: true }).fill('second');
  await page.getByRole('tab', { name: /^first/ }).click();
  await expect(page.getByLabel('Поисковый запрос', { exact: true })).toHaveValue('first');
  const token = (await (await page.request.get('/api/session')).json()).token;
  const headers = { 'X-Session-Token': token };
  const list = (await (await page.request.get('/api/search-tabs')).json());
  const removed = await page.request.delete(`/api/search-tabs/${list.active_tab_id}`, { headers });
  expect(removed.status()).toBe(204);
  await page.getByLabel('Поисковый запрос', { exact: true }).fill('late edit in deleted tab');
  await expect(page.getByRole('tab')).toHaveCount(1);
  await expect(page.getByLabel('Поисковый запрос', { exact: true })).toHaveValue('second');
  await page.getByRole('button', { name: 'Новая вкладка поиска', exact: true }).click();
  await expect(page.getByRole('tab')).toHaveCount(2);
  const older = (await (await page.request.get('/api/search-tabs')).json()).tabs.find((tab: { draft: { query: string } }) => tab.draft.query === 'second');
  await page.request.delete(`/api/search-tabs/${older.id}`, { headers });
  await page.getByRole('button', { name: 'Закрыть вкладку second', exact: true }).click();
  await expect(page.getByRole('tab')).toHaveCount(1);
  await expect(page.getByLabel('Поисковый запрос', { exact: true })).toHaveValue('');
});

test('valid large filter drafts save without the browser keepalive quota blocking switching', async ({ page }) => {
  const token = (await (await page.request.get('/api/session')).json()).token;
  const headers = { 'X-Session-Token': token };
  const created = await page.request.post('/api/search-tabs', { headers, data: { draft: {
    query: 'large', forwarded_from: Array.from({ length: 500 }, (_, i) => `Источник${i} ` + 'я'.repeat(128)),
  } } });
  const tab = await created.json();
  await page.goto('/');
  await page.getByLabel('Поисковый запрос', { exact: true }).fill('large saved');
  await expect.poll(async () => (await (await page.request.get(`/api/search-tabs/${tab.id}`)).json()).draft.query).toBe('large saved');
  await page.getByRole('button', { name: 'Новая вкладка поиска', exact: true }).click();
  await expect(page.getByRole('tab')).toHaveCount(2);
  await page.getByRole('tab', { name: /^large saved/ }).click();
  await expect(page.getByLabel('Поисковый запрос', { exact: true })).toHaveValue('large saved');
  await expect(page.locator('.error[role="alert"]')).toHaveCount(0);
});

test('history repeat failing during initial tab loading leaves the editor and retry available', async ({ page }) => {
  const token = (await (await page.request.get('/api/session')).json()).token;
  const headers = { 'X-Session-Token': token };
  await page.request.delete('/api/search-history/collections/favorites', { headers });
  await page.request.delete('/api/search-history/collections/history', { headers });
  const created = await page.request.post('/api/search-tabs', { headers, data: { draft: { query: 'initial draft' } } });
  const tab = await created.json();
  const submitted = await page.request.post('/api/search-jobs', { headers, data: { query: 'велосипед', mode: 'words', modalities: ['text'] } });
  const job = await submitted.json();
  await expect.poll(async () => (await (await page.request.get(`/api/search-jobs/${job.job_id}`)).json()).state).toBe('completed');
  let release!: () => void, held = false, runs = 0;
  await page.route(`**/api/search-tabs/${tab.id}`, async route => {
    if (route.request().method() !== 'GET' || held) return route.continue();
    held = true;
    const response = await route.fetch();
    await new Promise<void>(resolve => { release = resolve; });
    await route.fulfill({ response }).catch(() => {});
  });
  await page.route('**/api/search-tabs/*/jobs', route => ++runs === 1
    ? route.fulfill({ status: 409, json: { detail: 'Repeat refused' } }) : route.continue());
  try {
    await page.goto('/');
    await expect.poll(() => !!release).toBe(true);
    await page.getByRole('button', { name: 'История поиска', exact: true }).click();
    await page.getByRole('dialog').getByRole('button', { name: 'Повторить поиск', exact: true }).click();
    await expect(page.getByRole('alert')).toContainText('Repeat refused');
    release();
    await expect(page.getByLabel('Поисковый запрос', { exact: true })).toHaveValue('велосипед');
    await page.getByRole('button', { name: 'Найти', exact: true }).click();
    await expect(page.locator('.result-card')).toHaveCount(2);
  } finally { release?.(); }
});

test('paged results and scroll position remain independent across switching and reload', async ({ page }) => {
  const token = (await (await page.request.get('/api/session')).json()).token;
  const created = await page.request.post('/api/search-tabs', { headers: { 'X-Session-Token': token }, data: {
    draft: { query: 'велосипед', mode: 'words', modalities: ['text'], page_size: 1, chunk_size: 1 },
  } });
  const tab = await created.json();
  await page.goto('/');
  await page.getByRole('button', { name: 'Найти', exact: true }).click();
  await expect(page.locator('.result-card')).toHaveCount(1);
  await page.getByRole('button', { name: 'Показать ещё', exact: true }).click();
  await expect(page.locator('.result-card')).toHaveCount(2);
  await page.getByRole('tabpanel').evaluate(element => { element.scrollTop = 250; element.dispatchEvent(new Event('scroll')); });
  await expect.poll(async () => (await (await page.request.get(`/api/search-tabs/${tab.id}`)).json()).view.scroll_y).toBeGreaterThan(100);
  await page.getByRole('button', { name: 'Новая вкладка поиска', exact: true }).click();
  await expect(page.getByLabel('Поисковый запрос', { exact: true })).toHaveValue('');
  await page.getByRole('tab', { name: /^велосипед/ }).click();
  await expect(page.locator('.result-card')).toHaveCount(2);
  await expect.poll(() => page.getByRole('tabpanel').evaluate(element => element.scrollTop)).toBeGreaterThan(100);
  await page.reload();
  await expect(page.locator('.result-card')).toHaveCount(2);
  await expect.poll(() => page.getByRole('tabpanel').evaluate(element => element.scrollTop)).toBeGreaterThan(100);
});

test('removing a chat cannot reveal its already fetched late search result', async ({ page }) => {
  let release!: () => void, removed = false;
  await page.route('**/api/chats', async route => {
    const value = await (await route.fetch()).json();
    await route.fulfill({ json: removed ? value.filter((chat: { name: string }) => !chat.name.includes('мастерская')) : value });
  });
  await page.route('**/api/chats/*', route => {
    if (route.request().method() !== 'DELETE') return route.continue();
    removed = true;
    return route.fulfill({ json: { deleted: true } });
  });
  await page.route('**/api/search-jobs/*/result', async route => {
    const response = await route.fetch();
    await new Promise<void>(resolve => { release = resolve; });
    await route.fulfill({ response }).catch(() => {});
  });
  page.on('dialog', dialog => dialog.accept());
  try {
    await page.goto('/');
    await page.getByLabel('Поисковый запрос', { exact: true }).fill('велосипед');
    await page.getByRole('button', { name: 'Найти', exact: true }).click();
    await expect.poll(() => !!release).toBe(true);
    await page.getByRole('button', { name: 'Удалить Синтетический диалог: мастерская', exact: true }).click();
    await expect(page.locator('.chat-item').filter({ hasText: 'мастерская' })).toHaveCount(0);
    const delivered = page.waitForResponse('**/api/search-jobs/*/result');
    release(); await delivered;
    await expect(page.locator('.result-card')).toHaveCount(0);
    await expect(page.getByRole('button', { name: 'Найти', exact: true })).toBeEnabled();
  } finally { release?.(); }
});

test('clearing saved results while a completed response is in flight rejects that response', async ({ page }) => {
  let release!: () => void;
  await page.route('**/api/search-jobs/*/result', async route => {
    const response = await route.fetch();
    await new Promise<void>(resolve => { release = resolve; });
    await route.fulfill({ response }).catch(() => {});
  });
  try {
    await page.goto('/');
    await page.getByLabel('Поисковый запрос', { exact: true }).fill('велосипед');
    await page.getByRole('button', { name: 'Найти', exact: true }).click();
    await expect.poll(() => !!release).toBe(true);
    const token = (await (await page.request.get('/api/session')).json()).token;
    await page.request.delete('/api/search-history/collections/cache', { headers: { 'X-Session-Token': token } });
    release();
    await expect(page.getByRole('button', { name: 'Найти', exact: true })).toBeEnabled();
    await expect(page.locator('.result-card')).toHaveCount(0);
    await expect(page.locator('.snapshot-notice')).toContainText('повторите поиск');
  } finally { release?.(); }
});

test('each tab owns its queued progress and closing another tab cancels only its job', async ({ page }) => {
  const cancelled: string[] = [];
  let serial = 0;
  const status = (id: string) => ({ job_id: id, state: id === 'first' ? 'running' : 'queued',
    phase: id === 'first' ? 'query_text' : 'queued', completed: 0, total: null,
    unit: 'texts', elapsed_seconds: 1, rate: null, eta_seconds: null,
    queue_position: id === 'first' ? null : 1, warnings: [], error: null });
  await page.route('**/api/search-tabs/*/jobs', route => route.fulfill({ json: status(++serial === 1 ? 'first' : 'second') }));
  await page.route(/\/api\/search-jobs\/(first|second)$/, route => route.fulfill({ json: status(new URL(route.request().url()).pathname.split('/').at(-1)!) }));
  await page.route('**/api/search-jobs/*/cancel', route => {
    const id = new URL(route.request().url()).pathname.split('/').at(-2)!;
    cancelled.push(id); return route.fulfill({ json: { ...status(id), state: 'cancelled' } });
  });
  await page.goto('/');
  await page.getByLabel('Поисковый запрос', { exact: true }).fill('first');
  await page.getByRole('button', { name: 'Найти', exact: true }).click();
  await expect(page.getByText('Подготовка текстового запроса', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Новая вкладка поиска', exact: true }).click();
  await page.getByLabel('Поисковый запрос', { exact: true }).fill('second');
  await page.getByRole('button', { name: 'Найти', exact: true }).click();
  await expect(page.locator('.search-progress')).toContainText('Ожидание в очереди');
  await expect(page.locator('.search-progress')).toContainText('Место в очереди: 1');
  await page.getByRole('button', { name: 'Закрыть вкладку first', exact: true }).click();
  await expect(page.getByRole('tab')).toHaveCount(1);
  await expect.poll(() => cancelled).toEqual(['first']);
  await expect(page.getByRole('button', { name: 'Остановить', exact: true })).toBeEnabled();
  expect(serial).toBe(2);
});
