import { test, expect } from '@playwright/test';

test('search display settings persist and control result cards and their message windows', async ({ page }) => {
  let searches = 0;
  await page.route('**/api/search?**', route => { searches++; return route.continue(); });
  await page.goto('/');
  const token = (await (await page.request.get('/api/session')).json()).token;
  const original = await (await page.request.get('/api/settings')).json();
  try {
    const displayButton = page.getByRole('button', { name: 'Выдача поиска', exact: true });
    await expect(page.locator('.search-options')).toContainText('Выдача поиска');
    await expect(displayButton).toHaveAttribute('aria-expanded', 'false');
    await page.getByLabel('Поисковый запрос').fill('велосипед');
    await displayButton.click();
    await expect(displayButton).toHaveAttribute('aria-expanded', 'true');
    await page.getByLabel('Количество результатов', { exact: true }).fill('1');
    await page.getByLabel('Сообщений в одном фрагменте', { exact: true }).fill('1');
    await displayButton.click();
    await expect(page.getByLabel('Количество результатов', { exact: true })).toBeHidden();
    await displayButton.click();
    await expect(page.getByLabel('Количество результатов', { exact: true })).toHaveValue('1');
    await page.getByRole('button', { name: 'Сохранить настройки поиска' }).click();
    await expect(page.getByText(/Настройки поиска сохранены/)).toBeVisible();
    expect(searches).toBe(0);
    await page.getByRole('checkbox', { name: 'Изображения', exact: true }).uncheck();
    await page.getByRole('checkbox', { name: 'OCR', exact: true }).uncheck();
    await page.getByLabel('Поисковый запрос').fill('велосипед');
    await page.getByRole('button', { name: 'Найти' }).click();
    await expect(page.locator('.result-card')).toHaveCount(1);
    await expect(page.locator('.result-card .message')).toHaveCount(1);
    await expect(page.locator('.more-note')).toContainText('из лимита 1');
    await page.reload();
    await page.getByText('Выдача поиска', { exact: true }).click();
    await expect(page.getByLabel('Количество результатов', { exact: true })).toHaveValue('1');
    await expect(page.getByLabel('Сообщений в одном фрагменте', { exact: true })).toHaveValue('1');
    await page.getByLabel('Сообщений в одном фрагменте', { exact: true }).fill('101');
    await expect(page.getByRole('button', { name: 'Сохранить настройки поиска' })).toBeDisabled();
    await page.getByLabel('Количество результатов', { exact: true }).fill('5');
    await page.getByLabel('Сообщений в одном фрагменте', { exact: true }).fill('3');
    await page.getByRole('button', { name: 'Сохранить настройки поиска' }).click();
    await expect(page.getByText(/Настройки поиска сохранены/)).toBeVisible();
    expect(searches).toBe(1);
    await page.getByRole('checkbox', { name: 'Изображения', exact: true }).uncheck();
    await page.getByRole('checkbox', { name: 'OCR', exact: true }).uncheck();
    await page.getByLabel('Поисковый запрос').fill('велосипед');
    await page.getByRole('button', { name: 'Найти' }).click();
    await expect(page.locator('.result-card')).toHaveCount(2);
    const largerCard = page.locator('.result-card').filter({ has: page.locator('.message').nth(1) });
    await expect(largerCard).toHaveCount(1);
    await expect(largerCard.locator('.message')).toHaveCount(3);
    await expect(page.locator('.more-note')).toHaveCount(0);
    await largerCard.getByRole('button', { name: 'Открыть контекст' }).click();
    await expect(page.locator('.context-modal .message')).toHaveCount(4);
  } finally {
    const response = await page.request.patch('/api/settings', { headers: { 'X-Session-Token': token }, data: {
      search_result_limit: original.search_result_limit, display_chunk_size: original.display_chunk_size,
    } });
    expect(response.ok()).toBe(true);
  }
});

test('expanded context includes a large displayed card using its result snapshot', async ({ page }) => {
  const messages = Array.from({ length: 100 }, (_, index) => ({
    chat_id: 'context-smoke', message_id: index + 1, timestamp: 1750000000 + index,
    author: 'Синтетический автор', text: `проверка ${index + 1}`, kind: 'message', action: null,
    reply_to: null, forwarded_from: null, matches_filters: true, media: [], edited_timestamp: null,
  }));
  await page.route('**/api/search?**', route => route.fulfill({ json: {
    results: [{ chat_id: 'context-smoke', chat_name: 'Синтетический контекст', message_id: 1,
      timestamp: 1750000000, messages }], has_more: false, effective_mode: 'words', warnings: [], limit: 1,
  } }));
  await page.route('**/api/chats/context-smoke/context/**', async route => {
    const params = new URL(route.request().url()).searchParams;
    expect(params.get('before')).toBe('15');
    expect(params.get('after')).toBe('100');
    await route.fulfill({ json: { messages } });
  });
  await page.goto('/');
  await page.getByLabel('Поисковый запрос').fill('проверка');
  await page.getByRole('button', { name: 'Найти' }).click();
  await expect(page.locator('.result-card .message')).toHaveCount(100);
  await page.getByRole('button', { name: 'Открыть контекст' }).click();
  await expect(page.locator('.context-modal .message')).toHaveCount(100);
});

for (const otherFails of [false, true]) {
test(`slow index status${otherFails ? ' with another failed endpoint' : ''} does not stack refreshes or block saving 5/5 and sending a search`, async ({ page }) => {
  await page.clock.install({ time: new Date('2026-10-07T00:00:00Z') });
  await page.clock.pauseAt(new Date('2026-10-07T00:00:00Z'));
  let release!: () => void;
  let statuses = 0;
  let searches = 0;
  if (otherFails) {
    let imports = 0;
    await page.route('**/api/imports', route => ++imports === 1
      ? route.fulfill({ status: 500, json: { detail: 'Синтетическая ошибка статуса' } })
      : route.continue());
  }
  await page.route('**/api/semantic', async route => {
    statuses++;
    if (statuses === 1) await new Promise<void>(resolve => { release = resolve; });
    await route.fulfill({ json: { enabled: 0, runtime_installed: true } });
  });
  await page.route('**/api/search?**', route => {
    searches++;
    return route.fulfill({ json: { results: [], warnings: [], effective_mode: 'words', has_more: false, limit: 5 } });
  });
  await page.goto('/');
  const token = (await (await page.request.get('/api/session')).json()).token;
  const original = await (await page.request.get('/api/settings')).json();
  try {
    await expect.poll(() => !!release).toBe(true);
    await page.clock.runFor(12_000);
    expect(statuses).toBe(1);
    await page.getByRole('button', { name: 'Выдача поиска', exact: true }).click();
    await page.getByLabel('Количество результатов', { exact: true }).fill('5');
    await page.getByLabel('Сообщений в одном фрагменте', { exact: true }).fill('5');
    await page.getByRole('button', { name: 'Сохранить настройки поиска' }).click();
    await expect(page.getByText(/Настройки поиска сохранены/)).toBeVisible();
    await expect(page.getByRole('button', { name: 'Сохранить настройки поиска' })).toBeEnabled();
    const saved = await (await page.request.get('/api/settings')).json();
    expect([saved.search_result_limit, saved.display_chunk_size]).toEqual([5, 5]);
    await page.getByLabel('Поисковый запрос').fill('проверка');
    await page.getByRole('button', { name: 'Найти', exact: true }).click();
    await expect(page.getByText('Совпадений пока нет', { exact: true })).toBeVisible();
    expect(searches).toBe(1);
    release();
    if (otherFails) await expect(page.getByRole('alert')).toContainText('Синтетическая ошибка статуса');
    else await expect(page.locator('.chat-item')).not.toHaveCount(0);
    await page.clock.runFor(2000);
    await expect.poll(() => statuses).toBe(2);
    await expect(page.locator('.chat-item')).not.toHaveCount(0);
  } finally {
    release?.();
    const response = await page.request.patch('/api/settings', { headers: { 'X-Session-Token': token }, data: {
      search_result_limit: original.search_result_limit, display_chunk_size: original.display_chunk_size,
    } });
    expect(response.ok()).toBe(true);
  }
});
}
