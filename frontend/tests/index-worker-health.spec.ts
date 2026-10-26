import { expect, test } from '@playwright/test';

test('SQLite waits replace the stale indexing ETA and recover independently', async ({ page }) => {
  let waiting = true;
  await page.setViewportSize({ width: 390, height: 844 });
  await page.emulateMedia({ colorScheme: 'dark' });
  await page.route('**/api/indexing/health', route => route.fulfill({ json: { semantic: {
    enabled: true, alive: true, stopped: false, phase: waiting ? 'waiting_database' : 'indexing',
    waiting_reason: waiting ? 'database' : null, error: null,
    database_wait_seconds: 5, retry_in_seconds: 1, heartbeat_age_seconds: 0,
  } } }));
  await page.route('**/api/chats/*/index', async route => {
    const base = await (await route.fetch()).json();
    await route.fulfill({ json: { ...base, semantic: { ...base.semantic,
      enabled: 1, profile: 'small', preparation_state: 'ready', paused: 0,
      ready_segments: 0, total_segments: 1880, estimated_remaining_seconds: 120,
      estimate: { lower_seconds: 60, upper_seconds: 180 },
      works: [{ state: 'pending', count: 1880, chunks_total: 0, chunks_done: 0 }],
    } } });
  });
  await page.goto('/');
  await page.getByRole('button', { name: /^Настройки индексации / }).first().click();
  const text = page.getByRole('region', { name: 'Текст · E5' });
  await expect(text.getByText('Ожидание базы данных', { exact: true })).toBeVisible();
  await expect(text.getByRole('status')).toHaveText('База данных занята. Индексация продолжится автоматически.');
  await expect(text.locator('.index-eta')).toHaveCount(0);
  await page.getByRole('button', { name: 'RU', exact: true }).press('End');
  const english = page.getByRole('region', { name: 'Text · E5' });
  await expect(english.getByText('Waiting for the database', { exact: true })).toBeVisible();
  waiting = false;
  await expect(english.locator('.index-eta')).toHaveText('About 1–3 min remaining.');
  await expect(english.getByText('Indexing', { exact: true })).toBeVisible();
});

test('dead worker is visible while the ordinary status read is blocked and can be restarted', async ({ page }) => {
  let alive = false, restarts = 0;
  let release!: () => void;
  await page.route('**/api/indexing/health', route => route.fulfill({ json: { semantic: {
    enabled: true, alive, stopped: false, phase: alive ? 'idle' : 'stopped',
    waiting_reason: null, error: null, database_wait_seconds: 0,
    retry_in_seconds: 0, heartbeat_age_seconds: 0,
  } } }));
  await page.route('**/api/chats/*/index', async route => {
    await new Promise<void>(resolve => { release = resolve; });
    await route.continue();
  });
  await page.route('**/api/chats/*/index/text/retry', async route => {
    expect(route.request().method()).toBe('POST');
    restarts++; alive = true;
    await route.fulfill({ json: { semantic: {} } });
  });
  await page.goto('/');
  await page.getByRole('button', { name: /^Настройки индексации / }).first().click();
  await expect.poll(() => !!release).toBe(true);
  await expect(page.getByRole('alert')).toHaveText('Фоновый индексатор остановлен. Запустите его снова.');
  await page.getByRole('button', { name: 'Перезапустить индексатор', exact: true }).click();
  await expect.poll(() => restarts).toBe(1);
  await expect(page.getByText('Фоновый индексатор остановлен. Запустите его снова.', { exact: true })).toHaveCount(0);
  release();
  await page.keyboard.press('Escape');
  await expect(page.getByRole('dialog')).toHaveCount(0);
});
