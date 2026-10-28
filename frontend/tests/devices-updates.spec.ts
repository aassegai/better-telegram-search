import { expect, test } from './fixtures';

const update = {
  state: 'ready', error: null, current_version: '0.3.0', available_version: '0.4.0',
  current_variant: 'cpu', variant: 'cpu', notes: '### What\'s new\n\n- **GPU** indexing\n\n[Unsafe](javascript:alert(1))\n\n![image](https://example.invalid/track)\n\n<img src=x onerror=window.updateInjected=true>',
  completed_bytes: 100, total_bytes: 100, install_supported: true, gpu_build_supported: true,
};

test('photo-only search reports its query device without a text model', async ({ page }) => {
  await page.route('**/api/semantic', route => route.fulfill({ json: { runtime_installed: false } }));
  await page.route('**/api/media-index', route => route.fulfill({ json: {
    images_enabled: 1, backend: { device: 'gpu', provider: 'CUDAExecutionProvider' },
    query_backend: { device: 'gpu', provider: 'CUDAExecutionProvider' },
  } }));
  await page.goto('/');
  await page.getByText('Состояние индексов', { exact: true }).click();
  await expect(page.locator('.archive-status')).toContainText('Индекс: GPU · Поиск: GPU');
});

test('GPU indexing and CPU queries are independent and do not request reindexing', async ({ page }) => {
  await page.route('**/api/models/e5/device', async route => {
    const body = route.request().postDataJSON();
    expect(body).toEqual({ device: 'gpu', search_device: 'cpu' });
    expect(body.reindex).toBeUndefined();
    const models = await (await page.request.get('/api/models')).json();
    return route.fulfill({ json: { model: { ...models.e5, ...body }, execution: { provider: 'CUDAExecutionProvider' },
      query_execution: { provider: 'CPUExecutionProvider' } } });
  });
  await page.goto('/');
  await page.getByRole('button', { name: 'Настройки', exact: true }).click();
  const devices = page.getByRole('region', { name: 'Устройства текстовой модели', exact: true });
  await devices.getByLabel('Устройство для индексации', { exact: true }).selectOption('gpu');
  await expect(devices.getByLabel('Устройство для поиска', { exact: true })).toHaveValue('cpu');
  await expect(devices.locator('select')).toHaveCount(2);
  await expect(page.locator('.device-panel input')).toHaveCount(0);
  await devices.getByRole('button', { name: 'Сохранить устройства', exact: true }).click();
  await expect(page.getByText('Устройства сохранены.')).toBeVisible();
  await page.getByRole('button', { name: 'RU', exact: true }).press('End');
  const english = page.getByRole('region', { name: 'Text model devices', exact: true });
  await expect(english.getByLabel('Indexing device', { exact: true })).toHaveValue('gpu');
  await expect(english.getByLabel('Search device', { exact: true })).toHaveValue('cpu');
  await expect(page.getByRole('region', { name: 'Visual model devices', exact: true }).getByLabel('Indexing device')).toHaveValue('cpu');
});

test('changing update variant requires a new check and release notes remain escaped', async ({ page }) => {
  let variant = 'cpu';
  await page.route('**/api/updates', route => route.fulfill({ json: { ...update, variant } }));
  await page.route('**/api/updates/check', route => {
    expect(route.request().postDataJSON()).toEqual({ variant: 'gpu' });
    variant = 'gpu';
    return route.fulfill({ json: { ...update, variant } });
  });
  await page.goto('/');
  await page.getByRole('button', { name: 'Настройки', exact: true }).click();
  await page.getByRole('tab', { name: 'Обновления', exact: true }).click();
  const install = page.getByRole('button', { name: 'Обновить и перезапустить', exact: true });
  await expect(install).toBeEnabled();
  await page.getByText('Что изменилось', { exact: true }).click();
  await expect(page.locator('.release-notes h3')).toHaveText("What's new");
  await expect(page.locator('.release-notes li strong')).toHaveText('GPU');
  await expect(page.locator('.release-notes img')).toHaveCount(0);
  await expect(page.locator('.release-notes a[href^=javascript]')).toHaveCount(0);
  expect(await page.evaluate(() => (window as unknown as Record<string, unknown>).updateInjected)).toBeUndefined();
  await page.getByLabel('Вариант сборки', { exact: true }).selectOption('gpu');
  await expect(install).toBeDisabled();
  await page.getByRole('button', { name: 'Проверить обновления', exact: true }).click();
  await expect(install).toBeEnabled();
  await expect(page.getByText('Доступно: 0.4.0 · GPU')).toBeVisible();
});

test('install reconnects even if settings close and an old status response arrives late', async ({ page }) => {
  let state = 'ready';
  let hold = false;
  const release: (() => void)[] = [];
  await page.route('**/api/updates', async route => {
    if (hold) {
      await new Promise<void>(resolve => release.push(resolve));
      await route.fulfill({ json: { ...update, state: 'failed', error: 'Old response' } });
    } else await route.fulfill({ json: { ...update, state } });
  });
  await page.route('**/api/updates/install', route => {
    state = 'installing';
    return route.fulfill({ json: { ...update, state } });
  });
  await page.goto('/');
  await page.getByRole('button', { name: 'Настройки', exact: true }).click();
  await page.getByRole('tab', { name: 'Обновления', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Обновить и перезапустить', exact: true })).toBeEnabled();
  hold = true;
  await expect.poll(() => release.length).toBeGreaterThan(0);
  await page.getByRole('button', { name: 'Обновить и перезапустить', exact: true }).click();
  await page.getByRole('button', { name: 'Закрыть', exact: true }).click();
  hold = false;
  release.forEach(resolve => resolve());
  const reloaded = page.waitForEvent('framenavigated', { predicate: frame => frame === page.mainFrame() });
  state = 'updated';
  await reloaded;
  await expect(page.getByLabel('Поисковый запрос')).toBeVisible();
  await expect(page.getByText('Old response', { exact: true })).toHaveCount(0);
});


test('OCR model selection is saved separately from hybrid devices and preserves progress', async ({ page }) => {
  const models = await (await page.request.get('/api/models')).json();
  const selected = { ...models.ocr, device: 'cpu', engine: 'tesseract' };
  const writes: unknown[] = [];
  await page.route('**/api/models', route => route.fulfill({ json: { ...models, ocr: selected } }));
  await page.route('**/api/media-index', route => route.fulfill({ json: {
    ocr_runtime_installed: true, ocr_enabled: 1, ocr_ready: 234, ocr_failed: 0, total_photos: 45451, ocr_paused: 0,
    ocr_backend: { device: 'cpu+gpu', provider: 'CUDAExecutionProvider' },
  } }));
  await page.route('**/api/models/ocr/device', async route => {
    const body = route.request().postDataJSON();
    writes.push(body); Object.assign(selected, { device: body.device, engine: body.ocr_engine });
    await route.fulfill({ json: { model: selected,
      execution: { warning: null }, query_execution: { warning: null } } });
  });
  await page.goto('/');
  await page.getByText('Состояние индексов', { exact: true }).click();
  await expect(page.locator('.archive-status')).toContainText('Индекс: CPU + GPU');
  await page.getByRole('button', { name: 'Настройки', exact: true }).click();
  const ocr = page.getByRole('region', { name: 'Устройства OCR', exact: true });
  await expect(ocr.locator('option[value=hybrid]')).toHaveCount(0);
  await page.getByRole('tab', { name: 'Модели', exact: true }).click();
  const recognition = page.getByRole('region', { name: 'Модель распознавания · OCR', exact: true });
  await recognition.getByLabel('Модель OCR', { exact: true }).selectOption('paddle');
  expect(writes).toEqual([]);
  await recognition.getByRole('button', { name: 'Сохранить выбор OCR', exact: true }).click();
  await expect(recognition.getByRole('button', { name: 'Сохранить выбор OCR', exact: true })).toBeDisabled();
  expect(writes).toEqual([{ device: 'cpu', ocr_engine: 'paddle', defer_preparation: true }]);
  await page.getByRole('tab', { name: 'Производительность', exact: true }).click();
  await ocr.getByLabel('Устройство распознавания', { exact: true }).selectOption('hybrid');
  await expect(ocr.getByText('CPU и GPU распознают разные изображения одновременно. Пауза OCR останавливает оба устройства.')).toBeVisible();
  for (const name of ['Устройства текстовой модели', 'Устройства визуальной модели']) {
    await expect(page.getByRole('region', { name, exact: true }).locator('option[value=hybrid]')).toHaveCount(0);
  }
  await ocr.getByRole('button', { name: 'Сохранить устройства', exact: true }).click();
  await expect(ocr.getByRole('button', { name: 'Сохранить устройства', exact: true })).toBeDisabled();
  expect(writes[1]).toEqual({ device: 'hybrid', ocr_engine: 'paddle' });
  await page.getByRole('button', { name: 'RU', exact: true }).press('End');
  await expect(page.getByText('CPU and GPU recognize different images simultaneously. Pausing OCR stops both devices.')).toBeVisible();
  await page.getByRole('tab', { name: 'Models', exact: true }).click();
  await page.getByLabel('OCR model', { exact: true }).selectOption('tesseract');
  await page.getByRole('button', { name: 'Save OCR selection', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Save OCR selection', exact: true })).toBeDisabled();
  await page.getByRole('tab', { name: 'Performance', exact: true }).click();
  await expect(page.getByLabel('Recognition device', { exact: true })).toHaveValue('cpu');
  expect(writes[2]).toEqual({ device: 'cpu', ocr_engine: 'tesseract', defer_preparation: true });
});
