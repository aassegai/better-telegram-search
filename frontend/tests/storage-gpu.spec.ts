import { expect, test } from './fixtures';

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
  const resources = page.locator('.resource-settings');
  const gpu = page.getByRole('region', { name: 'Лимиты GPU', exact: true });
  await expect(resources.getByLabel('Потоков CPU', { exact: true })).toBeVisible();
  await expect(resources.getByRole('region', { name: 'Лимиты GPU', exact: true })).toBeVisible();
  await expect(resources.getByRole('button', { name: 'Сохранить ресурсы', exact: true })).toBeDisabled();
  await gpu.getByRole('button', { name: 'Экономный', exact: true }).click();
  await expect(gpu.getByRole('slider')).toHaveValue('25');
  await resources.getByRole('button', { name: 'Сохранить ресурсы', exact: true }).click();
  await expect(resources.getByRole('status')).toHaveText('Настройки ресурсов сохранены.');
  expect(writes).toEqual([{ gpu_index_load_percent: 25 }]);
  await gpu.getByRole('button', { name: '6144 МиБ', exact: true }).click();
  await resources.getByRole('button', { name: 'Сохранить ресурсы', exact: true }).click();
  await expect.poll(() => writes.length).toBe(2);
  expect(writes[1]).toEqual({ gpu_memory_limit_mib: 6144 });
  await resources.getByLabel('Потоков CPU', { exact: true }).fill('3');
  await gpu.getByRole('button', { name: 'Сбалансированный', exact: true }).click();
  await resources.getByRole('button', { name: 'Сохранить ресурсы', exact: true }).click();
  await expect.poll(() => writes.length).toBe(3);
  expect(writes[2]).toEqual({ cpu_threads: 3, gpu_index_load_percent: 60 });
  await resources.getByLabel('Потоков CPU', { exact: true }).fill('0');
  await expect(resources.getByRole('button', { name: 'Сохранить ресурсы', exact: true })).toBeDisabled();
  await resources.getByLabel('Потоков CPU', { exact: true }).fill('3');
  await page.setViewportSize({ width: 320, height: 640 });
  const modal = page.locator('.settings-overlay .modal');
  expect(await modal.evaluate(node => node.scrollWidth <= node.clientWidth + 1)).toBe(true);
  await page.getByRole('button', { name: 'RU', exact: true }).press('End');
  await expect(resources.getByRole('region', { name: 'GPU limits', exact: true })).toBeVisible();
  await expect(resources.getByRole('status')).toHaveText('Resource settings saved.');
});

test('vector cleanup confirms the kind, reports progress and refreshes retained indexes', async ({ page }) => {
  const chat = (await (await page.request.get('/api/chats')).json())[0];
  let job = { state: 'idle', kind: null as string | null, chat_id: chat.id, deleted_vectors: 0, freed_bytes: 0 };
  let polls = 0;
  let removed = false;
  let requests = 0;
  await page.route('**/api/chats/*/storage/vectors', route => route.fulfill({ json: { indexes: {
    text: { vectors: 3, reclaimable_vectors: 3, shared_vectors: 0, bytes: 1048576, shared: true },
    images: { vectors: removed ? 0 : 5, reclaimable_vectors: removed ? 0 : 5, shared_vectors: 0, bytes: removed ? 0 : 2097152, shared: false },
    ocr: { vectors: 2, reclaimable_vectors: 2, shared_vectors: 0, bytes: 1048576, shared: true },
  } } }));
  await page.route('**/api/storage/vectors/clear', route => {
    if (job.state === 'waiting' && ++polls >= 2) {
      removed = true; job = { state: 'done', kind: 'images', chat_id: chat.id, deleted_vectors: 5, freed_bytes: 2097152 };
    }
    return route.fulfill({ json: job });
  });
  await page.route('**/api/chats/*/storage/vectors/images/clear', route => {
    expect(decodeURIComponent(new URL(route.request().url()).pathname)).toBe(`/api/chats/${chat.id}/storage/vectors/images/clear`);
    expect(route.request().postDataJSON()).toEqual({ confirm: true });
    requests++; job = { ...job, state: 'waiting', kind: 'images' };
    return route.fulfill({ json: job });
  });
  await page.goto('/');
  await page.getByRole('button', { name: `Настройки индексации ${chat.name}`, exact: true }).click();
  const storage = page.getByRole('region', { name: 'Индексы этого диалога', exact: true });
  const images = storage.getByRole('region', { name: 'Векторы изображений', exact: true });
  await images.getByRole('button', { name: 'Очистить', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: 'Очистить векторный индекс?', exact: true });
  await expect(dialog.getByText('Векторы изображений', { exact: true })).toBeVisible();
  await expect(dialog).toContainText(`только для «${chat.name}»`);
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

test('storage controls wait for initial status and show completion before inventory settles', async ({ page }) => {
  const chat = (await (await page.request.get('/api/chats')).json())[0];
  let releaseInitial!: () => void;
  let releaseInventory!: () => void;
  const initial = new Promise<void>(resolve => { releaseInitial = resolve; });
  const pending = new Promise<void>(resolve => { releaseInventory = resolve; });
  let first = true;
  let done = false;
  let inventoryRequests = 0;
  await page.route('**/api/storage/vectors/clear', async route => {
    if (first) { first = false; await initial; }
    await route.fulfill({ json: { chat_id: chat.id, state: done ? 'done' : 'idle', kind: done ? 'text' : null, deleted_vectors: 1, freed_bytes: 1 } });
  });
  await page.route('**/api/storage', route => done
    ? route.fulfill({ status: 500, json: { detail: 'Synthetic space error' } })
    : route.fulfill({ json: { database_bytes: 0, vectors_bytes: 1, models_bytes: 0, cache_bytes: 0, reclaim_estimate_bytes: 0 } }));
  await page.route('**/api/chats/*/storage/vectors', async route => {
    if (done) { inventoryRequests++; await pending; }
    if (done) return route.fulfill({ status: 500, json: { detail: 'Synthetic inventory error' } });
    await route.fulfill({ json: { indexes: {
      text: { vectors: done ? 0 : 1, reclaimable_vectors: done ? 0 : 1, shared_vectors: 0, bytes: done ? 0 : 1, shared: false },
      images: { vectors: 0, reclaimable_vectors: 0, shared_vectors: 0, bytes: 0, shared: false }, ocr: { vectors: 0, reclaimable_vectors: 0, shared_vectors: 0, bytes: 0, shared: false },
    } } });
  });
  await page.route('**/api/chats/*/storage/vectors/text/clear', route => {
    done = true;
    return route.fulfill({ json: { state: 'waiting', kind: 'text', chat_id: chat.id } });
  });
  await page.goto('/');
  await page.getByRole('button', { name: `Настройки индексации ${chat.name}`, exact: true }).click();
  const storage = page.getByRole('region', { name: 'Индексы этого диалога', exact: true });
  const refresh = storage.getByRole('button', { name: 'Обновить сведения о месте', exact: true });
  await expect(refresh).toBeDisabled();
  releaseInitial();
  await expect(refresh).toBeEnabled();
  await storage.getByRole('region', { name: 'Текстовые векторы', exact: true }).getByRole('button', { name: 'Очистить', exact: true }).click();
  await page.getByRole('dialog', { name: 'Очистить векторный индекс?', exact: true }).getByRole('button', { name: 'Очистить векторы', exact: true }).click();
  await expect.poll(() => inventoryRequests).toBe(1);
  await expect(storage.getByRole('status')).toContainText('Индекс очищен.');
  await expect(storage.getByRole('region', { name: 'Текстовые векторы', exact: true }).getByRole('button', { name: 'Очистить', exact: true })).toBeEnabled();
  await page.waitForTimeout(1300);
  expect(inventoryRequests).toBe(1);
  releaseInventory();
  await expect(storage.getByRole('status')).toContainText('Индекс очищен.');
  await expect(refresh).toBeEnabled();
  await expect(storage.getByRole('alert')).toHaveText('Synthetic inventory error');
});

test('cleanup belongs to a named chat, shared-only vectors stay protected and labels follow language', async ({ page }) => {
  const chat = (await (await page.request.get('/api/chats')).json())[0];
  let requests = 0;
  await page.route('**/api/chats/*/storage/vectors', route => route.fulfill({ json: { indexes: {
    text: { vectors: 1, reclaimable_vectors: 1, shared_vectors: 0, bytes: 128, shared: true },
    images: { vectors: 2, reclaimable_vectors: 0, shared_vectors: 2, bytes: 0, shared: true },
    ocr: { vectors: 0, reclaimable_vectors: 0, shared_vectors: 0, bytes: 0, shared: true },
  } } }));
  await page.route('**/api/storage/vectors/clear', route => route.fulfill({ json: {
    state: 'done', chat_id: 'another-chat', kind: 'images', deleted_vectors: 999, freed_bytes: 999,
  } }));
  await page.route('**/api/chats/*/storage/vectors/*/clear', route => { requests++; return route.abort(); });
  await page.goto('/');
  await page.getByRole('button', { name: 'Настройки', exact: true }).click();
  await page.getByRole('tab', { name: 'База', exact: true }).click();
  await expect(page.locator('.vector-storage-card')).toHaveCount(0);
  await expect(page.getByRole('dialog')).toContainText('в настройках каждого диалога');
  await page.getByRole('button', { name: 'Закрыть', exact: true }).click();
  await page.getByRole('button', { name: `Настройки индексации ${chat.name}`, exact: true }).click();
  const storage = page.getByRole('region', { name: 'Индексы этого диалога', exact: true });
  await expect(storage.getByRole('region', { name: 'Векторы изображений', exact: true })).toContainText('общих с другими диалогами: 2');
  await expect(storage.getByRole('region', { name: 'Векторы изображений', exact: true }).getByRole('button')).toBeDisabled();
  await expect(storage.getByRole('status')).toHaveCount(0); // No success attributed to the wrong chat.
  await storage.getByRole('region', { name: 'Текстовые векторы', exact: true }).getByRole('button').click();
  await expect(page.getByRole('dialog', { name: 'Очистить векторный индекс?', exact: true })).toContainText(chat.name);
  await page.getByRole('button', { name: 'Отмена', exact: true }).click();
  expect(requests).toBe(0);
  await page.getByRole('button', { name: 'RU', exact: true }).press('End');
  const english = page.getByRole('region', { name: 'Indexes for this chat', exact: true });
  await expect(english).toContainText('shared with other chats: 2');
  await expect(english).not.toContainText('общих');
  await page.setViewportSize({ width: 320, height: 640 });
  const modal = page.locator('.settings-overlay .modal');
  expect(await modal.evaluate(node => node.scrollWidth <= node.clientWidth + 1)).toBe(true);
});

test('an active cleanup in another chat blocks actions without claiming its progress', async ({ page }) => {
  const chat = (await (await page.request.get('/api/chats')).json())[0];
  await page.route('**/api/storage/vectors/clear', route => route.fulfill({ json: {
    state: 'clearing', chat_id: 'another-chat', kind: 'images',
  } }));
  await page.goto('/');
  await page.getByRole('button', { name: `Настройки индексации ${chat.name}`, exact: true }).click();
  const storage = page.getByRole('region', { name: 'Индексы этого диалога', exact: true });
  await expect(storage.getByRole('status')).toHaveText('Выполняется очистка другого диалога. Дождитесь завершения.');
  for (const button of await storage.getByRole('button', { name: 'Очистить', exact: true }).all()) await expect(button).toBeDisabled();
  await page.getByRole('button', { name: 'Закрыть', exact: true }).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await page.getByRole('button', { name: `Настройки индексации ${chat.name}`, exact: true }).click();
  await expect(page.getByRole('region', { name: 'Индексы этого диалога', exact: true }).getByRole('status')).toHaveText('Выполняется очистка другого диалога. Дождитесь завершения.');
});

test('a failed disk cleanup can be retried after the logical vector count reaches zero', async ({ page }) => {
  const chat = (await (await page.request.get('/api/chats')).json())[0];
  let requests = 0;
  let job = { state: 'idle', kind: null as string | null, chat_id: chat.id, error: null as string | null };
  await page.route('**/api/storage/vectors/clear', route => route.fulfill({ json: job }));
  await page.route('**/api/chats/*/storage/vectors', route => route.fulfill({ json: { indexes: {
    text: { vectors: requests ? 0 : 1, reclaimable_vectors: requests ? 0 : 1, shared_vectors: 0, bytes: requests ? 0 : 1024, shared: true },
    images: { vectors: 0, reclaimable_vectors: 0, shared_vectors: 0, bytes: 0, shared: true },
    ocr: { vectors: 0, reclaimable_vectors: 0, shared_vectors: 0, bytes: 0, shared: true },
  } } }));
  await page.route('**/api/chats/*/storage/vectors/text/clear', route => {
    requests++;
    job = { state: requests === 1 ? 'failed' : 'done', kind: 'text', chat_id: chat.id,
      error: requests === 1 ? 'Не удалось очистить векторный индекс. Повторите очистку.' : null };
    return route.fulfill({ json: { ...job, deleted_vectors: 0, freed_bytes: 1024 } });
  });
  await page.goto('/');
  await page.getByRole('button', { name: `Настройки индексации ${chat.name}`, exact: true }).click();
  const storage = page.getByRole('region', { name: 'Индексы этого диалога', exact: true });
  await storage.getByRole('region', { name: 'Текстовые векторы', exact: true }).getByRole('button').click();
  await page.getByRole('dialog', { name: 'Очистить векторный индекс?', exact: true }).getByRole('button', { name: 'Очистить векторы', exact: true }).click();
  await expect(storage.getByRole('region', { name: 'Текстовые векторы', exact: true })).toContainText('Векторов: 0');
  await expect(storage.getByRole('button', { name: 'Повторить очистку', exact: true })).toBeEnabled();
  await page.getByRole('button', { name: 'RU', exact: true }).press('End');
  await page.getByRole('button', { name: 'Retry cleanup', exact: true }).click();
  await page.getByRole('dialog', { name: 'Clear the vector index?', exact: true }).getByRole('button', { name: 'Clear vectors', exact: true }).click();
  await expect.poll(() => requests).toBe(2);
  await expect(page.getByRole('region', { name: 'Indexes for this chat', exact: true }).getByRole('region', { name: 'Text vectors', exact: true }).getByRole('button')).toBeDisabled();
});

test('slow inventory does not block confirmed cleanup or accept its stale response', async ({ page }) => {
  const chat = (await (await page.request.get('/api/chats')).json())[0];
  let release!: () => void;
  const held = new Promise<void>(resolve => { release = resolve; });
  let inventoryRequests = 0;
  let clears = 0;
  let job = { state: 'idle', chat_id: chat.id, kind: null as string | null, deleted_vectors: 0 };
  const inventory = (vectors: number) => ({ indexes: Object.fromEntries(['text', 'images', 'ocr'].map(kind => [kind, {
    vectors, reclaimable_vectors: vectors, shared_vectors: 0, bytes: vectors * 1024, shared: false,
  }])) });
  await page.route('**/api/storage/vectors/clear', route => route.fulfill({ json: job }));
  await page.route('**/api/chats/*/storage/vectors', async route => {
    if (++inventoryRequests === 1) {
      await held;
      await route.fulfill({ json: inventory(99) }).catch(() => {});
    } else await route.fulfill({ json: inventory(0) });
  });
  await page.route('**/api/chats/*/storage/vectors/images/clear', route => {
    clears++;
    expect(route.request().postDataJSON()).toEqual({ confirm: true });
    job = { state: 'done', chat_id: chat.id, kind: 'images', deleted_vectors: 5 };
    return route.fulfill({ json: job });
  });
  await page.goto('/');
  await page.getByRole('button', { name: `Настройки индексации ${chat.name}`, exact: true }).click();
  const storage = page.getByRole('region', { name: 'Индексы этого диалога', exact: true });
  const images = storage.getByRole('region', { name: 'Векторы изображений', exact: true });
  await expect(storage.getByRole('status')).toHaveText('Подсчитываем размер индексов. Очистка уже доступна.');
  await images.getByRole('button', { name: 'Очистить', exact: true }).click();
  expect(clears).toBe(0);
  await page.getByRole('dialog', { name: 'Очистить векторный индекс?', exact: true }).getByRole('button', { name: 'Очистить векторы', exact: true }).click();
  await expect(images).toContainText('Векторов: 0');
  release();
  await expect(storage.getByRole('status')).toContainText('Удалено векторов: 5');
  await expect(images).not.toContainText('99');
  expect(clears).toBe(1);
});

test('failed inventory leaves cleanup available and permits a storage retry', async ({ page }) => {
  const chat = (await (await page.request.get('/api/chats')).json())[0];
  let attempts = 0;
  await page.route('**/api/storage/vectors/clear', route => route.fulfill({ json: { state: 'idle', chat_id: null, kind: null } }));
  await page.route('**/api/chats/*/storage/vectors', route => ++attempts === 1
    ? route.fulfill({ status: 500, json: { detail: 'Synthetic inventory failure' } })
    : route.fulfill({ json: { indexes: Object.fromEntries(['text', 'images', 'ocr'].map(kind => [kind, {
      vectors: 1, reclaimable_vectors: 1, shared_vectors: 0, bytes: 1024, shared: false,
    }])) } }));
  await page.goto('/');
  await page.getByRole('button', { name: `Настройки индексации ${chat.name}`, exact: true }).click();
  const storage = page.getByRole('region', { name: 'Индексы этого диалога', exact: true });
  await expect(storage.getByRole('alert')).toHaveText('Synthetic inventory failure');
  for (const button of await storage.getByRole('button', { name: 'Очистить', exact: true }).all()) await expect(button).toBeEnabled();
  await page.getByRole('button', { name: 'RU', exact: true }).press('End');
  const english = page.getByRole('region', { name: 'Indexes for this chat', exact: true });
  await expect(english).toContainText('Index sizes are not available yet. Cleanup is available; vectors shared with other chats will be kept.');
  await english.getByRole('button', { name: 'Refresh disk usage', exact: true }).click();
  await expect(english.getByRole('alert')).toHaveCount(0);
  await expect(english.getByRole('region', { name: 'Image vectors', exact: true })).toContainText('Vectors: 1');
  expect(attempts).toBe(2);
});

test('unknown cleanup status stays blocked and can be retried after a request failure', async ({ page }) => {
  const chat = (await (await page.request.get('/api/chats')).json())[0];
  let attempts = 0;
  await page.route('**/api/storage/vectors/clear', route => ++attempts === 1
    ? route.fulfill({ status: 500, json: { detail: 'Synthetic status failure' } })
    : route.fulfill({ json: { state: 'idle', kind: null, chat_id: null } }));
  await page.route('**/api/chats/*/storage/vectors', route => route.fulfill({ json: { indexes: Object.fromEntries(['text', 'images', 'ocr'].map(kind => [kind, {
    vectors: 1, reclaimable_vectors: 1, shared_vectors: 0, bytes: 1, shared: false,
  }])) } }));
  await page.goto('/');
  await page.getByRole('button', { name: `Настройки индексации ${chat.name}`, exact: true }).click();
  const storage = page.getByRole('region', { name: 'Индексы этого диалога', exact: true });
  await expect(storage.getByRole('alert')).toHaveText('Synthetic status failure');
  for (const button of await storage.getByRole('button', { name: 'Очистить', exact: true }).all()) await expect(button).toBeDisabled();
  await storage.getByRole('button', { name: 'Обновить сведения о месте', exact: true }).click();
  await expect(storage.getByRole('alert')).toHaveCount(0);
  for (const button of await storage.getByRole('button', { name: 'Очистить', exact: true }).all()) await expect(button).toBeEnabled();
});

test('stalled inventory times out visibly while cleanup stays available', async ({ page }) => {
  const chat = (await (await page.request.get('/api/chats')).json())[0];
  let release!: () => void;
  const held = new Promise<void>(resolve => { release = resolve; });
  let attempts = 0;
  await page.route('**/api/storage/vectors/clear', route => route.fulfill({ json: { state: 'idle', kind: null, chat_id: null } }));
  await page.route('**/api/chats/*/storage/vectors', async route => {
    if (++attempts === 1) await held;
    await route.fulfill({ json: { indexes: Object.fromEntries(['text', 'images', 'ocr'].map(kind => [kind, {
      vectors: 1, reclaimable_vectors: 1, shared_vectors: 0, bytes: 1, shared: false,
    }])) } }).catch(() => {});
  });
  await page.goto('/');
  await page.getByRole('button', { name: `Настройки индексации ${chat.name}`, exact: true }).click();
  const storage = page.getByRole('region', { name: 'Индексы этого диалога', exact: true });
  const refresh = storage.getByRole('button', { name: 'Обновить сведения о месте', exact: true });
  await expect(storage.getByRole('alert')).toContainText('Подсчёт размера индексов задерживается.', { timeout: 15000 });
  await expect(refresh).toBeEnabled();
  for (const button of await storage.getByRole('button', { name: 'Очистить', exact: true }).all()) await expect(button).toBeEnabled();
  await refresh.click();
  await expect(storage.getByRole('region', { name: 'Векторы изображений', exact: true })).toContainText('Векторов: 1');
  release();
  await expect(storage.getByRole('alert')).toHaveCount(0);
  expect(attempts).toBe(2);
});
