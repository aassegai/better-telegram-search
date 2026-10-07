import { expect, test } from '@playwright/test';

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
  await expect(page.locator('.pill')).toContainText('Индекс: GPU · Поиск: GPU');
});

test('GPU indexing and CPU queries are independent and do not request reindexing', async ({ page }) => {
  await page.route('**/api/device', route => {
    const body = route.request().postDataJSON();
    expect(body).toMatchObject({ device: 'gpu', search_device: 'cpu', gpu_memory_limit_mib: 4096 });
    expect(body.reindex).toBeUndefined();
    return route.fulfill({ json: { settings: body, execution: { provider: 'CUDAExecutionProvider' },
      query_execution: { provider: 'CPUExecutionProvider' } } });
  });
  await page.goto('/');
  await page.getByRole('button', { name: 'Настройки', exact: true }).click();
  await page.getByLabel('Устройство для индексации', { exact: true }).selectOption('gpu');
  await expect(page.getByLabel('Устройство для поиска', { exact: true })).toHaveValue('cpu');
  await expect(page.locator('.device-panel select')).toHaveCount(2);
  await expect(page.locator('.device-panel input')).toHaveCount(0);
  await page.getByRole('button', { name: 'Применить устройство', exact: true }).click();
  await expect(page.getByText('Устройства сохранены.')).toBeVisible();
  await page.getByRole('slider').press('End');
  await expect(page.getByLabel('Indexing device', { exact: true })).toHaveValue('gpu');
  await expect(page.getByLabel('Search device', { exact: true })).toHaveValue('cpu');
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
