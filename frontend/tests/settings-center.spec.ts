import { expect, test } from '@playwright/test';

test('four sections retain drafts, avoid accidental preparation and keep language accessible', async ({ page }) => {
  const mutations: string[] = [];
  page.on('request', request => { if (['POST', 'PATCH'].includes(request.method())) mutations.push(request.url()); });
  await page.goto('/');
  await page.getByRole('button', { name: 'Настройки', exact: true }).click();
  const dialog = page.getByRole('dialog');
  await expect(dialog).toHaveAccessibleName('Настройки');
  const tabs = dialog.getByRole('tablist', { name: 'Разделы настроек' });
  await expect(tabs.getByRole('tab')).toHaveCount(4);
  await expect(tabs.getByRole('tab', { name: 'Производительность', exact: true })).toHaveAttribute('aria-selected', 'true');
  await dialog.getByLabel('Потоков CPU', { exact: true }).fill('2');
  await tabs.getByRole('tab', { name: 'Модели', exact: true }).click();
  const text = dialog.getByRole('region', { name: 'Модель текста', exact: true });
  await text.getByLabel('Источник текстовой модели').selectOption('local');
  await text.getByLabel('Локальная папка модели', { exact: true }).fill('/synthetic/<script>/model');
  await tabs.getByRole('tab', { name: 'Производительность', exact: true }).click();
  await expect(dialog.getByLabel('Потоков CPU', { exact: true })).toHaveValue('2');
  await tabs.getByRole('tab', { name: 'Модели', exact: true }).click();
  await expect(text.getByLabel('Локальная папка модели', { exact: true })).toHaveValue('/synthetic/<script>/model');
  await dialog.getByRole('button', { name: 'EN', exact: true }).click({ position: { x: 2, y: 2 } });
  await expect(dialog.getByRole('tab', { name: 'Models', exact: true })).toHaveAttribute('aria-selected', 'true');
  await expect(dialog.getByLabel('Local model folder', { exact: true })).toHaveValue('/synthetic/<script>/model');
  expect(mutations).toEqual([]);
  await dialog.getByRole('button', { name: 'Close', exact: true }).click();
  await page.getByRole('button', { name: 'Settings', exact: true }).click();
  await expect(page.getByRole('tab', { name: 'Models', exact: true })).toHaveAttribute('aria-selected', 'true');
});

test('chat performance overrides and reset never replace global defaults', async ({ page }) => {
  await page.goto('/');
  const chat = (await (await page.request.get('/api/chats')).json())[0];
  const original = await (await page.request.get('/api/settings')).json();
  const token = (await (await page.request.get('/api/session')).json()).token;
  const path = `/api/chats/${chat.id}/performance`;
  const previous = await (await page.request.get(path)).json();
  try {
    await page.getByRole('button', { name: `Настройки индексации ${chat.name}`, exact: true }).click();
    await expect(page.getByRole('dialog').getByLabel('Батч текста', { exact: true })).toHaveCount(0);
    await page.getByRole('button', { name: 'Производительность этого диалога', exact: true }).click();
    await expect(page.getByLabel('Область настройки', { exact: true })).toHaveValue(chat.id);
    const field = page.getByRole('region', { name: 'Батч текста', exact: true });
    await field.getByLabel('Батч текста', { exact: true }).fill('32');
    await page.getByRole('button', { name: 'Сохранить настройки диалога', exact: true }).click();
    await expect(page.getByText('Настройки диалога сохранены.', { exact: true })).toBeVisible();
    let value = await (await page.request.get(path)).json();
    expect(value.batch_overrides.embedding_batch).toBe(32);
    expect((await (await page.request.get('/api/settings')).json()).embedding_batch).toBe(original.embedding_batch);
    await field.getByRole('button', { name: 'Использовать общее значение', exact: true }).click();
    await page.getByRole('button', { name: 'Сохранить настройки диалога', exact: true }).click();
    await expect(page.getByRole('button', { name: 'Сохранить настройки диалога', exact: true })).toBeDisabled();
    value = await (await page.request.get(path)).json();
    expect(value.batch_overrides.embedding_batch).toBeNull();
    expect(value.batches.embedding_batch).toBe(original.embedding_batch);
  } finally {
    await page.request.patch(path, { headers: { 'X-Session-Token': token }, data: previous.batch_overrides });
  }
});

test('date hit areas and language padding work without submitting or changing a model', async ({ page }) => {
  await page.addInitScript(() => {
    Object.defineProperty(HTMLInputElement.prototype, 'showPicker', { configurable: true, value() {
      this.dataset.pickerCalls = String(Number(this.dataset.pickerCalls ?? 0) + 1);
    } });
  });
  const mutations: string[] = [];
  page.on('request', request => { if (request.method() === 'POST') mutations.push(request.url()); });
  await page.goto('/');
  const from = page.getByLabel('С даты (UTC)', { exact: true });
  await from.click();
  await expect(from).toHaveAttribute('data-picker-calls', '1');
  await page.getByRole('button', { name: 'Открыть календарь: С даты (UTC)', exact: true }).click();
  await expect(from).toHaveAttribute('data-picker-calls', '2');
  await from.press('Enter');
  await expect(from).toHaveAttribute('data-picker-calls', '3');
  const to = page.getByLabel('По дату (UTC)', { exact: true });
  await page.getByRole('button', { name: 'Открыть календарь: По дату (UTC)', exact: true }).click();
  await expect(to).toHaveAttribute('data-picker-calls', '1');
  await from.fill('2026-01-01');
  await to.fill('2026-01-31');
  const language = page.getByRole('group', { name: 'Язык приложения' });
  const bounds = await language.boundingBox();
  await language.click({ position: { x: bounds!.width - 2, y: bounds!.height / 2 } });
  await expect(page.locator('html')).toHaveAttribute('lang', 'en');
  await page.getByRole('button', { name: 'EN', exact: true }).press('Home');
  await expect(page.locator('html')).toHaveAttribute('lang', 'ru');
  expect(mutations).toEqual([]);
});

test('a removed chat resets performance scope and late source lists choose an existing chat', async ({ page }) => {
  const original = await (await page.request.get('/api/chats')).json();
  let chats = original;
  await page.route('**/api/chats', route => route.fulfill({ json: chats }));
  await page.goto('/');
  await page.getByRole('button', { name: /^Настройки индексации / }).first().click();
  await page.getByRole('button', { name: 'Производительность этого диалога', exact: true }).click();
  await expect(page.getByLabel('Область настройки', { exact: true })).toHaveValue(original[0].id);
  chats = [];
  await expect(page.getByLabel('Область настройки', { exact: true })).toHaveValue('');
  await expect(page.getByLabel('Потоков CPU', { exact: true })).toBeVisible();
  await page.getByRole('tab', { name: 'База', exact: true }).click();
  await expect(page.getByLabel('Диалог источников', { exact: true })).toHaveCount(0);
  chats = original;
  await expect(page.getByLabel('Диалог источников', { exact: true })).toHaveValue(original[0].id);
  await page.getByText('Источники', { exact: true }).click();
  await expect(page.getByRole('button', { name: 'Проверить файлы', exact: true }).first()).toBeVisible();
});

test('preparation status belongs to the actual media model role', async ({ page }) => {
  let role: 'ocr' | 'images' = 'ocr';
  await page.route('**/api/media-index', async route => {
    const base = await (await route.fetch()).json();
    await route.fulfill({ json: { ...base, preparation_state: 'preparing', preparation_kind: role,
      images_enabled: 1, visual_profile: 'siglip2', images_available: true, ocr_available: true } });
  });
  await page.goto('/');
  await page.getByRole('button', { name: 'Настройки', exact: true }).click();
  await page.getByRole('tab', { name: 'Модели', exact: true }).click();
  const images = page.getByRole('region', { name: 'Модель изображений', exact: true });
  const ocr = page.getByRole('region', { name: 'Модель распознавания · OCR', exact: true });
  await expect(ocr.locator('.model-state')).toHaveText('Подготавливаем…');
  await expect(images.locator('.model-state')).toHaveText('Готова');
  role = 'images';
  await expect(images.locator('.model-state')).toHaveText('Подготавливаем…');
  await expect(ocr.locator('.model-state')).toHaveText('Готова');
  await expect(ocr.getByRole('button', { name: 'Подготовить OCR', exact: true })).toBeDisabled();
});

test('device-related resource changes refresh saved values without losing other resource drafts', async ({ page }) => {
  const models = await (await page.request.get('/api/models')).json();
  models.ocr = { ...models.ocr, engine: 'paddle', device: 'cpu' };
  let settings = { ...await (await page.request.get('/api/settings')).json(), ocr_cpu_workers: 2 };
  await page.route('**/api/models', route => route.fulfill({ json: models }));
  await page.route('**/api/settings', route => route.fulfill({ json: settings }));
  await page.route('**/api/models/ocr/device', route => {
    settings = { ...settings, ocr_cpu_workers: 1 };
    return route.fulfill({ json: { model: { ...models.ocr, device: 'gpu' },
      execution: { warning: null }, query_execution: { warning: null } } });
  });
  await page.goto('/');
  await page.getByRole('button', { name: 'Настройки', exact: true }).click();
  await expect(page.getByLabel('Воркеров OCR на CPU', { exact: true })).toHaveValue('2');
  await page.getByLabel('Потоков CPU', { exact: true }).fill('3');
  const ocr = page.getByRole('region', { name: 'Устройства OCR', exact: true });
  await ocr.getByLabel('Устройство распознавания', { exact: true }).selectOption('gpu');
  await ocr.getByRole('button', { name: 'Сохранить устройства', exact: true }).click();
  await expect(ocr.getByRole('button', { name: 'Сохранить устройства', exact: true })).toBeDisabled();
  await expect(page.getByLabel('Воркеров OCR на CPU', { exact: true })).toHaveValue('1');
  await expect(page.getByLabel('Потоков CPU', { exact: true })).toHaveValue('3');
  await expect(page.getByRole('button', { name: 'Сохранить ресурсы', exact: true })).toBeEnabled();
});
