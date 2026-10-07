import { expect, test } from '@playwright/test';
import type { Page } from '@playwright/test';

async function longArchive(page: Page) {
  const chats = Array.from({ length: 80 }, (_, index) => ({
    id: `layout-${index}`, name: `Диалог ${index + 1}`, scope: 'synthetic',
    messages: 100, photos: 0, date_from: null, date_to: null,
  }));
  const messages = Array.from({ length: 100 }, (_, index) => ({
    chat_id: chats[0].id, message_id: index + 1, timestamp: 1750000000 + index,
    author: 'Синтетический автор', text: `Сообщение ${index + 1}: ${'длинный фрагмент '.repeat(30)}`,
    kind: 'message', action: null, reply_to: null, forwarded_from: null,
    matches_filters: true, media: [], edited_timestamp: null,
  }));
  await page.route('**/api/chats', route => route.fulfill({ json: chats }));
  await page.route('**/api/imports', route => route.fulfill({ json: [] }));
  await page.route('**/api/import-previews', route => route.fulfill({ json: [] }));
  await page.route('**/api/authors?**', route => route.fulfill({ json: [] }));
  await page.route('**/api/search?**', route => route.fulfill({ json: {
    results: [{ chat_id: chats[0].id, chat_name: chats[0].name, message_id: 1,
      timestamp: messages[0].timestamp, messages }],
    warnings: [], has_more: false, effective_mode: 'words', limit: 1,
  } }));
  await page.goto('/');
  await page.getByLabel('Поисковый запрос').fill('фрагмент');
  await page.getByRole('button', { name: 'Найти', exact: true }).click();
  await expect(page.locator('.result-card .message')).toHaveCount(100);
  await expect(page.locator('.chat-item')).toHaveCount(80);
}

for (const viewport of [{ width: 1280, height: 720 }, { width: 900, height: 400 }]) {
  test(`settings stay accessible with long results and many chats at ${viewport.width}×${viewport.height}`, async ({ page }) => {
    await page.setViewportSize(viewport);
    await longArchive(page);
    const settings = page.getByRole('button', { name: 'Настройки', exact: true });
    await expect(settings).toBeInViewport({ ratio: 1 });
    const height = await page.evaluate(() => document.documentElement.scrollHeight);
    expect(height).toBeLessThanOrEqual(viewport.height + 1);
    await page.locator('.content').evaluate(element => { element.scrollTop = element.scrollHeight; });
    await expect(page.locator('.result-card .message').last()).toBeInViewport();
    await expect(settings).toBeInViewport({ ratio: 1 });
    // Keyboard navigation also scrolls the chat list without moving Settings.
    const lastChat = page.getByRole('button', { name: 'Настройки индексации Диалог 80', exact: true });
    await lastChat.focus();
    await expect(lastChat).toBeInViewport({ ratio: 1 });
    await expect(settings).toBeInViewport({ ratio: 1 });
    await settings.click();
    await expect(page.getByRole('dialog')).toBeVisible();
  });
}

for (const width of [320, 390]) {
  test(`mobile settings remain reachable after scrolling a long result at width ${width}`, async ({ page }) => {
    await page.setViewportSize({ width, height: 700 });
    await page.emulateMedia({ colorScheme: 'dark' });
    await longArchive(page);
    await page.evaluate(() => window.scrollTo(0, document.documentElement.scrollHeight));
    await expect(page.locator('.result-card .message').last()).toBeInViewport();
    const settings = page.getByRole('button', { name: 'Настройки', exact: true });
    await expect(settings).toBeInViewport({ ratio: 1 });
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width);
    expect(await settings.evaluate(element => {
      const rect = element.getBoundingClientRect();
      return element.contains(document.elementFromPoint(rect.x + rect.width / 2, rect.y + rect.height / 2));
    })).toBe(true);
    await settings.click();
    await expect(page.getByRole('dialog')).toBeInViewport();
    await page.getByRole('button', { name: 'Закрыть', exact: true }).click();
    await expect(settings).toBeInViewport({ ratio: 1 });
  });
}

for (const viewport of [{ width: 1280, height: 720 }, { width: 900, height: 400 }, { width: 320, height: 700 }]) {
  test(`settings close stays outside the scrolling panel at ${viewport.width}×${viewport.height}`, async ({ page }) => {
    await page.setViewportSize(viewport);
    await page.goto('/');
    const settings = page.getByRole('button', { name: 'Настройки', exact: true });
    await settings.click();
    const dialog = page.getByRole('dialog', { name: 'Настройки и диагностика' });
    const panel = dialog.locator('.modal');
    const close = dialog.getByRole('button', { name: 'Закрыть', exact: true });
    await expect(panel.getByRole('heading', { name: 'Обновления приложения' })).toBeAttached();
    const initial = (await close.boundingBox())!;
    const bounds = (await panel.boundingBox())!;
    expect(initial.y + initial.height).toBeLessThanOrEqual(bounds.y);
    await panel.evaluate(element => { element.scrollTop = element.scrollHeight; });
    expect(await panel.evaluate(element => element.scrollTop)).toBeGreaterThan(0);
    await expect(close).toBeInViewport({ ratio: 1 });
    expect((await close.boundingBox())!.y).toBeCloseTo(initial.y, 1);
    await close.click();
    await expect(dialog).toHaveCount(0);
    await expect(settings).toBeFocused();
    await page.getByRole('button', { name: /Настройки индексации / }).first().click();
    const index = page.getByRole('dialog');
    await expect(index.locator('.index-card')).toHaveCount(3);
    const indexClose = index.getByRole('button', { name: 'Закрыть', exact: true });
    const indexInitial = (await indexClose.boundingBox())!;
    const indexBounds = (await index.locator('.modal').boundingBox())!;
    expect(indexInitial.y + indexInitial.height).toBeLessThanOrEqual(indexBounds.y);
    await index.locator('.modal').evaluate(element => { element.scrollTop = element.scrollHeight; });
    await expect(indexClose).toBeInViewport({ ratio: 1 });
    expect((await indexClose.boundingBox())!.y).toBeCloseTo(indexInitial.y, 1);
    await page.keyboard.press('Escape');
    await expect(index).toHaveCount(0);
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(viewport.width);
  });
}
