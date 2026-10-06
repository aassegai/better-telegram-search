import { test, expect } from '@playwright/test';

test('search display settings persist and control result cards and their message windows', async ({ page }) => {
  await page.goto('/');
  const token = (await (await page.request.get('/api/session')).json()).token;
  const original = await (await page.request.get('/api/settings')).json();
  try {
    await page.getByRole('button', { name: '⚙ Настройки и диагностика' }).click();
    await page.getByLabel('Количество результатов', { exact: true }).fill('1');
    await page.getByLabel('Сообщений в одном фрагменте', { exact: true }).fill('1');
    await page.getByRole('button', { name: 'Сохранить настройки поиска' }).click();
    await expect(page.getByText(/Настройки поиска сохранены/)).toBeVisible();
    await page.getByRole('button', { name: 'Закрыть', exact: true }).click();
    await page.getByRole('checkbox', { name: 'Изображения', exact: true }).uncheck();
    await page.getByRole('checkbox', { name: 'OCR', exact: true }).uncheck();
    await page.getByLabel('Поисковый запрос').fill('велосипед');
    await page.getByRole('button', { name: 'Найти' }).click();
    await expect(page.locator('.result-card')).toHaveCount(1);
    await expect(page.locator('.result-card .message')).toHaveCount(1);
    await expect(page.locator('.more-note')).toContainText('из лимита 1');
    await page.reload();
    await page.getByRole('button', { name: '⚙ Настройки и диагностика' }).click();
    await expect(page.getByLabel('Количество результатов', { exact: true })).toHaveValue('1');
    await expect(page.getByLabel('Сообщений в одном фрагменте', { exact: true })).toHaveValue('1');
    await page.getByLabel('Сообщений в одном фрагменте', { exact: true }).fill('101');
    await expect(page.getByRole('button', { name: 'Сохранить настройки поиска' })).toBeDisabled();
    await page.getByLabel('Количество результатов', { exact: true }).fill('5');
    await page.getByLabel('Сообщений в одном фрагменте', { exact: true }).fill('3');
    await page.getByRole('button', { name: 'Сохранить настройки поиска' }).click();
    await expect(page.getByText(/Настройки поиска сохранены/)).toBeVisible();
    await page.getByRole('button', { name: 'Закрыть', exact: true }).click();
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
