import { expect, test } from '@playwright/test';

test('GPU presets save live load and per-model memory without changing devices', async ({ page }) => {
  let settings = await (await page.request.get('/api/settings')).json();
  settings = { ...settings, gpu_memory_limit_mib: 4096, gpu_index_load_percent: 100 };
  const writes: unknown[] = [];
  await page.route('**/api/settings', async route => {
    if (route.request().method() === 'PATCH') {
      const body = route.request().postDataJSON();
      writes.push(body); settings = { ...settings, ...body };
    }
    await route.fulfill({ json: settings });
  });
  await page.goto('/');
  await page.getByRole('button', { name: 'Настройки', exact: true }).click();
  await page.getByText('Производительность GPU', { exact: true }).click();
  const gpu = page.getByRole('region', { name: 'Лимиты GPU', exact: true });
  await gpu.getByRole('button', { name: 'Экономный', exact: true }).click();
  await expect(gpu.getByRole('slider')).toHaveValue('25');
  await gpu.getByRole('button', { name: 'Сохранить лимиты GPU', exact: true }).click();
  await expect(gpu.getByRole('status')).toHaveText('Лимиты GPU сохранены.');
  expect(writes).toEqual([{ gpu_index_load_percent: 25 }]);
  await gpu.getByRole('button', { name: '6144 МиБ', exact: true }).click();
  await gpu.getByRole('button', { name: 'Сохранить лимиты GPU', exact: true }).click();
  await expect.poll(() => writes.length).toBe(2);
  expect(writes[1]).toEqual({ gpu_index_load_percent: 25, gpu_memory_limit_mib: 6144 });
  await page.setViewportSize({ width: 320, height: 640 });
  const modal = page.locator('.settings-overlay .modal');
  expect(await modal.evaluate(node => node.scrollWidth <= node.clientWidth + 1)).toBe(true);
  await page.locator('.language-switch input').press('End');
  await expect(page.getByRole('region', { name: 'GPU limits', exact: true }).getByRole('status')).toHaveText('GPU limits saved.');
});

test('vector cleanup confirms the kind, reports progress and refreshes retained indexes', async ({ page }) => {
  let job = { state: 'idle', kind: null as string | null, deleted_vectors: 0, freed_bytes: 0 };
  let polls = 0;
  let removed = false;
  let requests = 0;
  await page.route('**/api/storage/vectors', route => route.fulfill({ json: { indexes: {
    text: { vectors: 3, bytes: 1048576, shared: true },
    images: { vectors: removed ? 0 : 5, bytes: removed ? 0 : 2097152, shared: false },
    ocr: { vectors: 2, bytes: 1048576, shared: true },
  } } }));
  await page.route('**/api/storage/vectors/clear', route => {
    if (job.state === 'waiting' && ++polls >= 2) {
      removed = true; job = { state: 'done', kind: 'images', deleted_vectors: 5, freed_bytes: 2097152 };
    }
    return route.fulfill({ json: job });
  });
  await page.route('**/api/storage/vectors/images/clear', route => {
    expect(route.request().postDataJSON()).toEqual({ confirm: true });
    requests++; job = { ...job, state: 'waiting', kind: 'images' };
    return route.fulfill({ json: job });
  });
  await page.goto('/');
  await page.getByRole('button', { name: 'Настройки', exact: true }).click();
  const storage = page.getByRole('region', { name: 'Место на диске', exact: true });
  const images = storage.getByRole('region', { name: 'Векторы изображений', exact: true });
  await images.getByRole('button', { name: 'Очистить', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: 'Очистить векторный индекс?', exact: true });
  await expect(dialog.getByText('Векторы изображений', { exact: true })).toBeVisible();
  await dialog.getByRole('button', { name: 'Отмена', exact: true }).click();
  expect(requests).toBe(0);
  await images.getByRole('button', { name: 'Очистить', exact: true }).click();
  await dialog.getByRole('button', { name: 'Очистить векторы', exact: true }).click();
  await expect(storage.getByText('Ожидаем завершения текущего батча…', { exact: true })).toBeVisible();
  await expect(images.getByRole('button', { name: 'Очистить', exact: true })).toBeDisabled();
  await expect(storage.getByRole('status')).toContainText('Удалено векторов: 5. Освобождено: 2.0 МиБ.');
  await expect(images).toContainText('Векторов: 0');
  await expect(storage.getByRole('region', { name: 'Векторы текста OCR', exact: true })).toContainText('Векторов: 2');
  await expect(storage.getByRole('region', { name: 'Текстовые векторы', exact: true }).getByRole('button', { name: 'Очистить', exact: true })).toBeEnabled();
  expect(requests).toBe(1);
});

test('storage controls wait for initial status and terminal refresh settles before retry', async ({ page }) => {
  let releaseInitial!: () => void;
  let releaseInventory!: () => void;
  const initial = new Promise<void>(resolve => { releaseInitial = resolve; });
  const pending = new Promise<void>(resolve => { releaseInventory = resolve; });
  let first = true;
  let done = false;
  let inventoryRequests = 0;
  await page.route('**/api/storage/vectors/clear', async route => {
    if (first) { first = false; await initial; }
    await route.fulfill({ json: { state: done ? 'done' : 'idle', kind: done ? 'text' : null, deleted_vectors: 1, freed_bytes: 1 } });
  });
  await page.route('**/api/storage', route => done
    ? route.fulfill({ status: 500, json: { detail: 'Synthetic space error' } })
    : route.fulfill({ json: { database_bytes: 0, vectors_bytes: 1, models_bytes: 0, cache_bytes: 0, reclaim_estimate_bytes: 0 } }));
  await page.route('**/api/storage/vectors', async route => {
    if (done) { inventoryRequests++; await pending; }
    await route.fulfill({ json: { indexes: {
      text: { vectors: done ? 0 : 1, bytes: done ? 0 : 1, shared: false },
      images: { vectors: 0, bytes: 0, shared: false }, ocr: { vectors: 0, bytes: 0, shared: false },
    } } });
  });
  await page.route('**/api/storage/vectors/text/clear', route => {
    done = true;
    return route.fulfill({ json: { state: 'waiting', kind: 'text' } });
  });
  await page.goto('/');
  await page.getByRole('button', { name: 'Настройки', exact: true }).click();
  const storage = page.getByRole('region', { name: 'Место на диске', exact: true });
  const refresh = storage.getByRole('button', { name: 'Обновить сведения о месте', exact: true });
  await expect(refresh).toBeDisabled();
  releaseInitial();
  await expect(refresh).toBeEnabled();
  await storage.getByRole('region', { name: 'Текстовые векторы', exact: true }).getByRole('button', { name: 'Очистить', exact: true }).click();
  await page.getByRole('dialog', { name: 'Очистить векторный индекс?', exact: true }).getByRole('button', { name: 'Очистить векторы', exact: true }).click();
  await expect.poll(() => inventoryRequests).toBe(1);
  await page.waitForTimeout(1300);
  expect(inventoryRequests).toBe(1);
  releaseInventory();
  await expect(storage.getByRole('status')).toContainText('Индекс очищен.');
  await expect(refresh).toBeEnabled();
  await expect(storage.getByRole('alert')).toHaveText('Synthetic space error');
});
