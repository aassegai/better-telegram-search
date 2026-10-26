import { expect, test } from '@playwright/test';

test('empty query browses messages explicitly and an empty chat selection never means all', async ({ page }) => {
  const specs: { query: string; scope: { mode: string; chat_ids?: string[] } }[] = [];
  await page.route('**/api/search-jobs', route => {
    specs.push(route.request().postDataJSON());
    return route.continue();
  });
  await page.goto('/');
  await expect(page.getByRole('button', { name: 'Показать сообщения', exact: true })).toBeEnabled();
  expect(specs).toHaveLength(0);
  await page.getByRole('button', { name: 'Показать сообщения', exact: true }).click();
  await expect(page.locator('.result-card').first()).toBeVisible();
  for (const card of await page.locator('.result-card').all()) {
    await expect(card.locator('.message')).toHaveCount(1);
  }
  expect(specs[0]).toMatchObject({ query: '', scope: { mode: 'all' } });
  for (const checkbox of await page.locator('.chat-list input[type=checkbox]').all()) {
    await checkbox.uncheck();
  }
  expect(specs).toHaveLength(1);
  await page.getByRole('button', { name: 'Показать сообщения', exact: true }).click();
  await expect(page.getByText('Не выбрано ни одного диалога.', { exact: true })).toBeVisible();
  await expect(page.locator('.result-card')).toHaveCount(0);
  expect(specs[1].scope).toEqual({ mode: 'selected', chat_ids: [] });
});

test('measured job progress and native-call cancellation keep the previous completed results', async ({ page }) => {
  await page.goto('/');
  await page.getByLabel('Поисковый запрос').fill('велосипед');
  await page.getByRole('button', { name: 'Найти', exact: true }).click();
  await expect(page.locator('.result-card')).toHaveCount(2);
  let completed = 25;
  let state = 'running';
  let cancelCalls = 0;
  const status = () => ({ job_id: 'controlled', state, phase: 'image_vectors',
    completed, total: 100, unit: 'vectors', elapsed_seconds: 2, rate: 12.5,
    eta_seconds: 6, queue_position: null, error: null, warnings: [] });
  await page.route('**/api/search-jobs', route => route.fulfill({ json: status() }));
  await page.route('**/api/search-jobs/controlled', route => route.fulfill({ json: status() }));
  await page.route('**/api/search-jobs/controlled/cancel', route => {
    expect(route.request().method()).toBe('POST');
    cancelCalls++; state = 'cancelling';
    return route.fulfill({ json: status() });
  });
  await page.getByLabel('Поисковый запрос').fill('a long image search');
  await page.getByRole('button', { name: 'Найти', exact: true }).click();
  const progress = page.getByRole('progressbar', { name: 'Прогресс текущего этапа' });
  await expect(progress).toHaveAttribute('value', '25');
  await expect(progress).toHaveAttribute('max', '100');
  completed = 75;
  await expect(progress).toHaveAttribute('value', '75');
  await page.getByRole('button', { name: 'Остановить', exact: true }).click();
  await expect(page.getByText('Останавливаем…', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Остановить', exact: true })).toBeDisabled();
  await expect(page.locator('.result-card')).toHaveCount(2);
  await expect(page.locator('.submitted-search')).toHaveText('Результаты запроса: велосипед');
  expect(cancelCalls).toBe(1);
  state = 'cancelled';
  await expect(page.getByText('Поиск остановлен', { exact: true })).toBeVisible();
  await expect(progress).toHaveCount(0);
  await expect(page.locator('.result-card')).toHaveCount(2);
  await expect(page.getByRole('button', { name: 'Найти', exact: true })).toBeEnabled();
});

test('slow media diagnostics do not hold back chats or the search form', async ({ page }) => {
  let release!: () => void;
  await page.route('**/api/media-index', async route => {
    await new Promise<void>(resolve => { release = resolve; });
    await route.continue();
  });
  await page.goto('/');
  try {
    await expect.poll(() => !!release).toBe(true);
    await expect(page.locator('.chat-item')).not.toHaveCount(0);
    await expect(page.getByRole('button', { name: 'Показать сообщения', exact: true })).toBeEnabled();
    await page.getByRole('button', { name: 'Показать сообщения', exact: true }).click();
    await expect(page.locator('.result-card').first()).toBeVisible();
  } finally {
    release?.();
  }
});

for (const failed of [false, true]) {
  test(`late ${failed ? 'failed' : 'successful'} cancellation cannot change a new job`, async ({ page }) => {
    let state = 'running';
    let current = 'first';
    let release!: () => void;
    const status = (id: string, value = state) => ({ job_id: id, state: value, phase: 'query_text',
      completed: 0, total: null, unit: 'texts', elapsed_seconds: 1, rate: null,
      eta_seconds: null, queue_position: null, error: null, warnings: [] });
    await page.route('**/api/search-jobs', route => route.fulfill({ json: status(current, 'running') }));
    await page.route('**/api/search-jobs/first', route => route.fulfill({ json: status('first') }));
    await page.route('**/api/search-jobs/second', route => route.fulfill({ json: status('second', 'running') }));
    await page.route('**/api/search-jobs/first/cancel', async route => {
      state = 'cancelled';
      await new Promise<void>(resolve => { release = resolve; });
      await route.fulfill(failed ? { status: 500, json: { detail: 'Stale cancellation error' } }
        : { json: status('first', 'cancelling') });
    });
    await page.goto('/');
    await page.getByLabel('Поисковый запрос').fill('first');
    await page.getByRole('button', { name: 'Найти', exact: true }).click();
    await page.getByRole('button', { name: 'Остановить', exact: true }).click();
    await expect(page.getByText('Поиск остановлен', { exact: true })).toBeVisible();
    current = 'second';
    await page.getByLabel('Поисковый запрос').fill('second');
    await page.getByRole('button', { name: 'Найти', exact: true }).click();
    await expect(page.getByText('Подготовка текстового запроса', { exact: true })).toBeVisible();
    const response = page.waitForResponse('**/api/search-jobs/first/cancel');
    release(); await response;
    await page.waitForResponse('**/api/search-jobs/second');
    await expect(page.getByRole('button', { name: 'Остановить', exact: true })).toBeEnabled();
    await expect(page.getByText('Останавливаем…', { exact: true })).toHaveCount(0);
    await expect(page.getByText('Stale cancellation error', { exact: true })).toHaveCount(0);
  });
}

test('failed cancellation returns to running and can be retried', async ({ page }) => {
  const status = { job_id: 'retry', state: 'running', phase: 'query_text', completed: 0,
    total: null, unit: 'texts', elapsed_seconds: 1, rate: null, eta_seconds: null,
    queue_position: null, error: null, warnings: [] };
  let calls = 0;
  await page.route('**/api/search-jobs', route => route.fulfill({ json: status }));
  await page.route('**/api/search-jobs/retry', route => route.fulfill({ json: status }));
  await page.route('**/api/search-jobs/retry/cancel', route => {
    calls++;
    if (calls === 1) return route.fulfill({ status: 500, json: { detail: 'Try stop again' } });
    status.state = 'cancelled';
    return route.fulfill({ json: status });
  });
  await page.goto('/');
  await page.getByLabel('Поисковый запрос').fill('retry stop');
  await page.getByRole('button', { name: 'Найти', exact: true }).click();
  const stop = page.getByRole('button', { name: 'Остановить', exact: true });
  await stop.click();
  await expect(page.getByRole('alert')).toContainText('Try stop again');
  await expect(stop).toBeEnabled();
  await stop.click();
  await expect(page.getByText('Поиск остановлен', { exact: true })).toBeVisible();
  expect(calls).toBe(2);
});
