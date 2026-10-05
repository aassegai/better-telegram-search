import { test, expect } from '@playwright/test';
import path from 'node:path';

test('search, conjunctive author filter and context preserve markers', async ({ page }) => {
  await page.goto('/');
  await page.getByLabel('Поисковый запрос').fill('велосипед');
  await page.getByRole('button', { name: 'Найти' }).click();
  await expect(page.locator('.result-card')).toHaveCount(2);
  await page.getByLabel('Автор', { exact: true }).selectOption('bob');
  await page.getByRole('button', { name: 'Найти' }).click();
  await expect(page.locator('.result-card')).toHaveCount(1);
  await page.getByRole('button', { name: 'Открыть контекст' }).click();
  await expect(page.getByRole('dialog')).toBeVisible();
  await expect(page.locator('.context-modal .context-tag').first()).toContainText('вне фильтра');
  await page.getByRole('button', { name: 'Закрыть контекст' }).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
});

test('exact phrase and escaped export text', async ({ page }) => {
  await page.goto('/');
  await page.getByLabel('Поисковый запрос').fill('красный велосипед');
  await page.getByLabel('Точная фраза').check();
  await page.getByRole('button', { name: 'Найти' }).click();
  await expect(page.locator('.result-card')).toHaveCount(1);
  await page.getByLabel('Точная фраза').uncheck();
  await page.getByLabel('Поисковый запрос').fill('literal');
  await page.getByRole('button', { name: 'Найти' }).click();
  await expect(page.locator('.result-card')).toContainText('<script>window.syntheticInjected');
  expect(await page.evaluate(() => (window as unknown as Record<string, unknown>).syntheticInjected)).toBeUndefined();
});

test('local path import and new authors appear without reloading', async ({ page }) => {
  await page.goto('/');
  await page.getByRole('button', { name: 'Импортировать экспорт' }).click();
  await page.getByLabel('Путь к JSON').fill(path.resolve('../tests/fixtures/synthetic/third-chat.json'));
  await page.getByRole('button', { name: 'Начать импорт' }).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await expect(page.locator('.chat-item')).toHaveCount(3);
  await expect(page.getByLabel('Автор', { exact: true }).locator('option[value="gleb"]')).toHaveCount(1);
});

test('late context response cannot reopen a dismissed dialog', async ({ page }) => {
  await page.goto('/');
  await page.getByLabel('Поисковый запрос').fill('красный');
  await page.getByRole('button', { name: 'Найти' }).click();
  await expect(page.locator('.result-card')).toHaveCount(1);
  let complete: () => void = () => {};
  const waiting = new Promise<void>(resolve => { complete = resolve; });
  await page.route('**/context/**', async route => { await waiting; await route.continue(); });
  await page.getByRole('button', { name: 'Открыть контекст' }).click();
  await page.keyboard.press('Escape');
  complete();
  await page.waitForResponse(response => response.url().includes('/context/'));
  await expect(page.getByRole('dialog')).toHaveCount(0);
});
