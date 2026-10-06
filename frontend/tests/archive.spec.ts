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
  await page.getByRole('button', { name: 'Проверить экспорт', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Отчёт готов' })).toBeVisible();
  await expect(page.locator('.chat-item')).toHaveCount(2);
  await page.getByRole('button', { name: 'Применить изменения' }).click();
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

test('preview survives closing and conflicts can be compared and resolved safely', async ({ page }) => {
  await page.goto('/');
  await page.getByRole('button', { name: 'Импортировать экспорт' }).click();
  await page.getByLabel('Путь к JSON').fill(path.resolve('../tests/fixtures/synthetic/conflict-chat.json'));
  await page.getByRole('button', { name: 'Проверить экспорт', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Отчёт готов' })).toBeVisible();
  await expect(page.locator('.preview-report')).toContainText('Конфликты');
  await page.getByRole('button', { name: 'Закрыть', exact: true }).click();
  await page.getByRole('button', { name: 'Открыть отчёт' }).click();
  await page.getByRole('button', { name: 'Применить изменения' }).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await page.getByRole('button', { name: 'Разобрать конфликты' }).click();
  await expect(page.locator('.conflict-versions')).toContainText('В субботу поедем на озеро.');
  await expect(page.locator('.conflict-versions')).toContainText('<img src=x onerror=window.conflictInjected=true>');
  expect(await page.evaluate(() => (window as unknown as Record<string, unknown>).conflictInjected)).toBeUndefined();
  await page.getByRole('button', { name: 'Использовать версию из экспорта' }).click();
  await expect(page.getByRole('dialog')).toContainText('Все конфликты разрешены.');
  await page.getByRole('button', { name: 'Закрыть конфликты' }).click();
  await page.getByLabel('Поисковый запрос').fill('воскресенье');
  await page.getByRole('button', { name: 'Найти' }).click();
  await expect(page.locator('.result-card')).toHaveCount(1);
});

test('late apply response cannot close a newly opened settings dialog', async ({ page }) => {
  await page.goto('/');
  await page.getByRole('button', { name: 'Импортировать экспорт' }).click();
  await page.getByLabel('Путь к JSON').fill(path.resolve('../tests/fixtures/synthetic/third-chat.json'));
  await page.getByRole('button', { name: 'Проверить экспорт', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Отчёт готов' })).toBeVisible();
  let release: () => void = () => {};
  let received: () => void = () => {};
  const waiting = new Promise<void>(resolve => { release = resolve; });
  const requestReceived = new Promise<void>(resolve => { received = resolve; });
  await page.route('**/api/import-previews/*/apply', async route => {
    const response = await route.fetch(); received();
    await waiting; await route.fulfill({ response });
  });
  await page.getByRole('button', { name: 'Применить изменения' }).click();
  await requestReceived;
  await page.keyboard.press('Escape');
  await page.getByRole('button', { name: 'Настройки и диагностика' }).click();
  const finished = page.waitForResponse(response => response.url().endsWith('/apply'));
  release(); await finished;
  await expect(page.getByRole('heading', { name: 'Настройки и диагностика' })).toBeVisible();
});

test('pause blocks concurrent discard and resumed preview preserves counts', async ({ page }) => {
  await page.goto('/');
  await page.route('**/api/import-previews', async route => {
    if (route.request().method() !== 'POST') { await route.continue(); return; }
    const response = await route.fetch();
    await route.fulfill({ response, json: { ...await response.json(), state: 'running' } });
  });
  await page.getByRole('button', { name: 'Импортировать экспорт' }).click();
  await page.getByLabel('Путь к JSON').fill(path.resolve('../tests/fixtures/synthetic/third-chat.json'));
  let release: () => void = () => {};
  const waiting = new Promise<void>(resolve => { release = resolve; });
  await page.route('**/api/import-previews/*/pause', async route => {
    const response = await route.fetch(); await waiting; await route.fulfill({ response });
  });
  await page.getByRole('button', { name: 'Проверить экспорт', exact: true }).click();
  await page.getByRole('button', { name: 'Пауза', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Проверить другой экспорт' })).toBeDisabled();
  release();
  await page.getByRole('button', { name: 'Продолжить проверку' }).click();
  await expect(page.getByRole('heading', { name: 'Отчёт готов' })).toBeVisible();
  await expect(page.locator('.preview-report')).toContainText('Без изменений');
  await page.getByRole('button', { name: 'Проверить другой экспорт' }).click();
  await expect(page.getByLabel('Путь к JSON')).toBeVisible();
});
