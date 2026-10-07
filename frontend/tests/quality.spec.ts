import { expect, test } from '@playwright/test';

test('model settings expose independent devices, GPU OCR and actual local paths; sources belong to chats', async ({ page }) => {
  const models = await (await page.request.get('/api/models')).json();
  await page.goto('/');
  await page.getByRole('button', { name: 'Настройки', exact: true }).click();
  for (const name of ['E5', 'CLIP', 'OCR']) {
    const panel = page.getByRole('region', { name: `Устройства ${name}`, exact: true });
    await expect(panel).toBeVisible();
    await expect(panel.locator('.model-paths')).toContainText(models[name.toLowerCase()].paths[0]);
  }
  await expect(page.getByText('Источники', { exact: true })).toHaveCount(0);
  const ocr = page.getByRole('region', { name: 'Устройства OCR', exact: true });
  await ocr.getByLabel('Устройство распознавания').selectOption('gpu');
  await expect(ocr.getByLabel('Модель OCR')).toHaveValue('paddle');
  await page.getByRole('button', { name: 'Закрыть', exact: true }).click();
  await page.getByRole('button', { name: /^Настройки индексации / }).first().click();
  await page.getByText('Источники', { exact: true }).click();
  await expect(page.getByRole('button', { name: 'Проверить файлы', exact: true }).first()).toBeVisible();
});

test('OCR pause keeps image indexing active and uses its own endpoint', async ({ page }) => {
  let paused = false;
  let changed = false;
  const media = { ocr_enabled: 1, images_enabled: 1, paused: 0, ocr_paused: 0,
    ocr_ready: 1, ocr_failed: 0, total_photos: 100, images_ready: 10,
    preparation_state: 'ready', ocr_dense_available: false, ocr_runtime_installed: true };
  await page.route('**/api/chats/*/index', async route => {
    const value = await (await route.fetch()).json();
    await route.fulfill({ json: { ...value, media: { ...value.media, ...media, ocr_paused: Number(paused) } } });
  });
  await page.route('**/api/chats/*/index/ocr/pause', async route => {
    expect(route.request().method()).toBe('POST'); paused = true; changed = true;
    const value = await (await route.fetch()).json();
    await route.fulfill({ json: { ...value, media: { ...value.media, ...media, ocr_paused: 1 } } });
  });
  await page.goto('/');
  await page.getByRole('button', { name: /^Настройки индексации / }).first().click();
  await page.getByRole('button', { name: 'Пауза OCR', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Продолжить OCR', exact: true })).toBeVisible();
  expect(changed).toBe(true);
  await expect(page.getByRole('region', { name: 'Изображения · CLIP' }).getByRole('button', { name: 'Пауза индексации', exact: true })).toBeVisible();
});

for (const fails of [false, true]) {
  test(`search progress remains visible until ${fails ? 'failure' : 'completion'}`, async ({ page }) => {
    let release!: () => void;
    await page.route('**/api/search?**', async route => {
      await new Promise<void>(resolve => { release = resolve; });
      await route.fulfill(fails ? { status: 400, json: { detail: 'Синтетическая ошибка' } }
        : { json: { results: [], warnings: [], has_more: false, effective_mode: 'words', limit: 20 } });
    });
    await page.goto('/');
    await page.getByLabel('Поисковый запрос').fill('прогресс');
    await page.getByRole('button', { name: 'Найти', exact: true }).click();
    const progress = page.getByRole('progressbar', { name: 'Выполнение поиска' });
    await expect(progress).toBeVisible();
    await expect(progress).not.toHaveAttribute('value');
    await expect.poll(() => !!release).toBe(true);
    release();
    await expect(progress).toHaveCount(0);
    await expect(page.getByRole('button', { name: 'Найти', exact: true })).toBeEnabled();
  });
}

test('dark theme respects system preference, keeps controls readable and preserves explicit choice', async ({ page }) => {
  await page.emulateMedia({ colorScheme: 'dark' });
  await page.goto('/');
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
  await page.getByRole('button', { name: 'Настройки', exact: true }).click();
  const contrast = await page.locator('.device-panel select').first().evaluate(element => {
    const css = getComputedStyle(element);
    const luminance = (color: string) => color.match(/\d+/g)!.slice(0, 3).map(Number)
      .map(value => value / 255).map(value => value <= .04045 ? value / 12.92 : ((value + .055) / 1.055) ** 2.4)
      .reduce((sum, value, index) => sum + value * [.2126, .7152, .0722][index], 0);
    const a = luminance(css.color), b = luminance(css.backgroundColor);
    return (Math.max(a, b) + .05) / (Math.min(a, b) + .05);
  });
  expect(contrast).toBeGreaterThan(4.5);
  await page.screenshot({ path: '../workspace/theme-033-dark.png', fullPage: true });
  await page.getByRole('button', { name: 'Закрыть', exact: true }).click();
  const searchButton = page.getByRole('button', { name: 'Найти', exact: true });
  await searchButton.hover();
  const hoverContrast = await searchButton.evaluate(element => {
    const css = getComputedStyle(element);
    const luminance = (color: string) => color.match(/\d+/g)!.slice(0, 3).map(Number)
      .map(value => value / 255).map(value => value <= .04045 ? value / 12.92 : ((value + .055) / 1.055) ** 2.4)
      .reduce((sum, value, index) => sum + value * [.2126, .7152, .0722][index], 0);
    const a = luminance(css.color), b = luminance(css.backgroundColor);
    return (Math.max(a, b) + .05) / (Math.min(a, b) + .05);
  });
  expect(hoverContrast).toBeGreaterThan(4.5);
  await page.getByRole('button', { name: 'Тёмная тема', exact: true }).click();
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'light');
  await page.reload();
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'light');
  await page.getByRole('slider').press('End');
  await expect(page.getByRole('button', { name: 'Dark theme', exact: true })).toHaveAttribute('aria-pressed', 'false');
});

test('closing settings invalidates the device follow-up response', async ({ page }) => {
  await page.clock.install({ time: new Date('2026-10-07T00:00:00Z') });
  await page.clock.pauseAt(new Date('2026-10-07T00:00:00Z'));
  const models = await (await page.request.get('/api/models')).json();
  const original = await (await page.request.get('/api/media-index')).json();
  let delayed = false;
  let armed = false;
  let release!: () => void;
  await page.route('**/api/models/clip/device', async route => {
    armed = true;
    await route.fulfill({ json: { model: models.clip, execution: { warning: null }, query_execution: { warning: null } } });
  });
  await page.route('**/api/media-index', async route => {
    if (armed && !delayed) {
      delayed = true;
      await new Promise<void>(resolve => { release = resolve; });
      await route.fulfill({ json: { ...original, error: 'STALE FOLLOW-UP' } });
    } else await route.fulfill({ json: original });
  });
  await page.goto('/');
  await page.getByRole('button', { name: 'Настройки', exact: true }).click();
  await page.getByRole('region', { name: 'Устройства CLIP', exact: true })
    .getByRole('button', { name: 'Применить устройство', exact: true }).click();
  await expect.poll(() => !!release).toBe(true);
  await page.keyboard.press('Escape');
  await page.getByRole('button', { name: 'Настройки', exact: true }).click();
  const response = page.waitForResponse(response => response.url().endsWith('/api/media-index'));
  release(); await response;
  await expect(page.getByText('STALE FOLLOW-UP', { exact: true })).toHaveCount(0);
});
