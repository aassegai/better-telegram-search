import { expect, test } from './fixtures';
import type { Page } from '@playwright/test';

async function prepare(page: Page) {
  const token = (await (await page.request.get('/api/session')).json()).token;
  const headers = { 'X-Session-Token': token };
  await page.request.delete('/api/search-history/collections/favorites', { headers });
  await page.request.delete('/api/search-history/collections/history', { headers });
  return headers;
}

for (const english of [false, true]) {
  test(`history restores saved results and a yellow favorite independently of the draft (${english ? 'EN' : 'RU'})`, async ({ page }) => {
    if (english) await page.addInitScript(() => localStorage.setItem('bts.language', 'en'));
    const headers = await prepare(page);
    const original = await (await page.request.get('/api/settings')).json();
    await page.request.patch('/api/settings', { headers, data: { search_result_limit: 1, display_chunk_size: 1 } });
    let searches = 0;
    await page.route('**/api/search-tabs/*/jobs', route => { searches++; return route.continue(); });
    try {
      await page.goto('/');
      await page.getByRole('checkbox', { name: english ? 'Images' : 'Изображения', exact: true }).uncheck();
      await page.getByRole('checkbox', { name: 'OCR', exact: true }).uncheck();
      await page.getByLabel(english ? 'Search query' : 'Поисковый запрос', { exact: true }).fill('велосипед');
      await page.getByRole('button', { name: english ? 'Search' : 'Найти', exact: true }).click();
      await expect(page.locator('.result-card')).toHaveCount(1);
      // The star belongs to the completed search even while its draft changes.
      await page.getByLabel(english ? 'Search query' : 'Поисковый запрос', { exact: true }).fill('неисполненный черновик');
      await page.locator('.submitted-search-actions').getByRole('button', {
        name: english ? 'Add to favorites' : 'Добавить в избранное', exact: true,
      }).click();
      const favorite = page.locator('.submitted-search-actions .favorite-button');
      await expect(favorite).toHaveAttribute('aria-pressed', 'true');
      await expect(favorite.locator('svg')).toHaveCSS('fill', 'rgb(255, 212, 71)');
      await page.reload();
      expect(searches).toBe(1);
      await page.getByRole('button', { name: english ? 'Search history' : 'История поиска', exact: true }).click();
      const dialog = page.getByRole('dialog');
      await expect(dialog.locator('.saved-search')).toHaveCount(1);
      await expect(dialog.locator('.saved-search-heading')).toContainText('велосипед');
      await expect(dialog).not.toContainText('неисполненный черновик');
      await expect(dialog.getByRole('button', { name: english ? 'Remove from favorites' : 'Убрать из избранного', exact: true })).toHaveAttribute('aria-pressed', 'true');
      await dialog.getByRole('button', { name: english ? 'Open search' : 'Открыть поиск', exact: true }).click();
      await expect(page.getByRole('dialog')).toHaveCount(0);
      await expect(page.getByLabel(english ? 'Search query' : 'Поисковый запрос', { exact: true })).toHaveValue('велосипед');
      await expect(page.locator('.result-card')).toHaveCount(1);
      await page.getByRole('button', { name: english ? 'Show more' : 'Показать ещё', exact: true }).click();
      await expect(page.locator('.result-card')).toHaveCount(2);
      expect(searches).toBe(1);
      await page.getByRole('button', { name: english ? 'Repeat search' : 'Повторить поиск', exact: true }).click();
      await expect.poll(() => searches).toBe(2);
      await expect(page.locator('.result-card')).toHaveCount(1);
      await expect(page.locator('.submitted-search-actions .favorite-button')).toHaveAttribute('aria-pressed', 'true');
    } finally {
      await page.request.patch('/api/settings', { headers, data: {
        search_result_limit: original.search_result_limit, display_chunk_size: original.display_chunk_size,
      } });
    }
  });
}

test('stale history restores its conditions without showing old messages or running a search', async ({ page }) => {
  await prepare(page);
  let searches = 0;
  await page.route('**/api/search-tabs/*/jobs', route => { searches++; return route.continue(); });
  await page.goto('/');
  await page.getByLabel('Поисковый запрос', { exact: true }).fill('велосипед');
  await page.getByRole('button', { name: 'Найти', exact: true }).click();
  await expect(page.locator('.result-card')).toHaveCount(2);
  const entries = (await (await page.request.get('/api/search-history')).json()).entries;
  const token = (await (await page.request.get('/api/session')).json()).token;
  expect(entries.length).toBeGreaterThan(0);
  const cleared = await page.request.delete('/api/search-history/collections/cache', { headers: { 'X-Session-Token': token } });
  expect(cleared.ok()).toBeTruthy();
  await page.getByLabel('Поисковый запрос', { exact: true }).fill('черновик');
  await page.getByRole('button', { name: 'История поиска', exact: true }).click();
  await page.getByRole('dialog').getByRole('button', { name: 'Открыть поиск', exact: true }).click();
  await expect(page.getByLabel('Поисковый запрос', { exact: true })).toHaveValue('велосипед');
  await expect(page.locator('.result-card')).toHaveCount(0);
  await expect(page.locator('.snapshot-notice')).toContainText('повторите поиск');
  expect(searches).toBe(1);
});

test('late history response cannot replace a new search after closing its dialog', async ({ page }) => {
  await prepare(page);
  await page.goto('/');
  await page.getByLabel('Поисковый запрос', { exact: true }).fill('велосипед');
  await page.getByRole('button', { name: 'Найти', exact: true }).click();
  await expect(page.locator('.result-card')).toHaveCount(2);
  const entry = (await (await page.request.get('/api/search-history')).json()).entries[0];
  let release: () => void = () => {}, received: () => void = () => {};
  const waiting = new Promise<void>(resolve => { release = resolve; });
  const entered = new Promise<void>(resolve => { received = resolve; });
  await page.route(`**/api/search-history/${entry.id}`, async route => {
    const response = await route.fetch(); received(); await waiting; await route.fulfill({ response });
  });
  try {
    await page.getByRole('button', { name: 'История поиска', exact: true }).click();
    await page.getByRole('dialog').getByRole('button', { name: 'Открыть поиск', exact: true }).click();
    await entered;
    await page.keyboard.press('Escape');
    await page.getByLabel('Поисковый запрос', { exact: true }).fill('literal');
    await page.getByRole('button', { name: 'Найти', exact: true }).click();
    await expect(page.locator('.submitted-search')).toHaveText('Результаты запроса: literal');
    const accepted = await page.locator('.result-card').allTextContents();
    expect(accepted.length).toBeGreaterThan(0);
    const finished = page.waitForResponse(response => new URL(response.url()).pathname.endsWith(entry.id));
    release(); await finished;
    await expect(page.getByLabel('Поисковый запрос', { exact: true })).toHaveValue('literal');
    await expect(page.locator('.result-card')).toHaveText(accepted);
    await expect(page.getByRole('dialog')).toHaveCount(0);
  } finally { release(); }
});

test('database settings clear saved results while retaining starred queries', async ({ page }) => {
  await prepare(page);
  await page.goto('/');
  await page.getByLabel('Поисковый запрос', { exact: true }).fill('велосипед');
  await page.getByRole('button', { name: 'Найти', exact: true }).click();
  await expect(page.locator('.result-card')).toHaveCount(2);
  await page.locator('.submitted-search-actions').getByRole('button', { name: 'Добавить в избранное', exact: true }).click();
  await expect(page.locator('.submitted-search-actions .favorite-button')).toHaveAttribute('aria-pressed', 'true');
  await page.getByRole('button', { name: 'Настройки', exact: true }).click();
  await page.getByRole('tab', { name: 'База', exact: true }).click();
  page.on('dialog', dialog => dialog.accept());
  await page.getByRole('button', { name: 'Очистить сохранённую выдачу', exact: true }).click();
  await expect(page.getByText('Сохранённые поиски обновлены.', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Закрыть', exact: true }).click();
  await expect(page.locator('.result-card')).toHaveCount(0);
  await page.getByRole('button', { name: 'История поиска', exact: true }).click();
  const dialog = page.getByRole('dialog');
  await expect(dialog.locator('.saved-search')).toHaveCount(1);
  await expect(dialog.getByRole('button', { name: 'Убрать из избранного', exact: true })).toHaveAttribute('aria-pressed', 'true');
  await expect(dialog).toContainText('Сохранённая выдача недоступна');
});
