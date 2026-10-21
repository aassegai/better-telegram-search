import { expect, test } from '@playwright/test';

test('memory waits explain the app threshold and stop promising an ETA', async ({ page }) => {
  let mode: 'waiting' | 'paused' | 'running' = 'waiting';
  await page.route('**/api/chats/*/index', async route => {
    const base = await (await route.fetch()).json();
    const estimate = { lower_seconds: 120, upper_seconds: 240,
      waiting_reason: mode === 'waiting' ? 'memory' : null, paused: mode === 'paused' };
    await route.fulfill({ json: { ...base, media: { ...base.media,
      visual_profile: 'siglip2', images_enabled: 1, ocr_enabled: 1, preparation_state: 'ready',
      paused: Number(mode === 'paused'), ocr_paused: Number(mode === 'paused'),
      ocr_dense_paused: Number(mode === 'paused'), total_photos: 100, images_ready: 20,
      ocr_ready: 20, ocr_failed: 0, ocr_nonempty_ready: 10, ocr_dense_ready: 5,
      ocr_dense_available: true, images_estimated_remaining_seconds: 180,
      ocr_estimated_remaining_seconds: 180, ocr_dense_estimated_remaining_seconds: 180,
      images_estimate: estimate, ocr_estimate: estimate, ocr_dense_estimate: estimate,
      preparation_estimated_remaining_seconds: null, preparation_estimate: estimate,
      memory_wait: mode === 'waiting' ? { stages: ['clip', 'ocr', 'ocr_dense'],
        rss_bytes: 6000 * 1024 ** 2, limit_bytes: 4096 * 1024 ** 2, available_bytes: 20000 * 1024 ** 2 } : null,
      resource_error: mode === 'waiting' ? 'Достигнут предел RAM: медиа ожидает освобождения памяти.' : null,
    } } });
  });
  await page.goto('/');
  await page.getByRole('button', { name: /^Настройки индексации / }).first().click();
  const images = page.getByRole('region', { name: 'Изображения · SigLIP 2' });
  const ocr = page.getByRole('region', { name: 'Текст на изображениях · OCR' });
  await expect(images.getByText('Ожидание RAM', { exact: true })).toBeVisible();
  await expect(images.locator('.index-eta')).toHaveText('Оценка времени недоступна: индексация ожидает памяти.');
  await expect(images.getByText('RAM приложения: 6000 МиБ · порог: 4096 МиБ.')).toBeVisible();
  await expect(ocr.getByText('Свободно в системе: 20000 МиБ.')).toBeVisible();
  await expect(ocr.locator('.index-eta')).toHaveText([
    'Оценка времени недоступна: индексация ожидает памяти.',
    'Оценка времени недоступна: индексация ожидает памяти.',
  ]);
  await expect(images.getByText('Достигнут предел RAM: медиа ожидает освобождения памяти.')).toHaveCount(0);
  await page.getByRole('slider').press('End');
  const english = page.getByRole('region', { name: 'Images · SigLIP 2' });
  await expect(english.getByText('Waiting for RAM', { exact: true })).toBeVisible();
  await expect(english.getByText('App RAM: 6000 MiB · threshold: 4096 MiB.')).toBeVisible();
  mode = 'paused';
  await expect(english.locator('.index-eta')).toHaveText('Resume indexing to estimate the remaining time.');
  await expect(english.locator('.memory-wait')).toHaveCount(0);
  mode = 'running';
  await expect(english.locator('.index-eta')).toHaveText('About 2–4 min remaining.');
  await expect(english).toContainText('20 / 100');
  mode = 'waiting';
  await english.getByRole('button', { name: 'Open general settings', exact: true }).click();
  await page.locator('.resource-settings').getByText('Shared indexing resources', { exact: true }).click();
  await expect(page.getByLabel('Media RAM threshold (MiB)', { exact: true })).toBeVisible();
});

test('index ETA shows ranges, stale measurements and paused pipelines in both languages', async ({ page }) => {
  let mode: 'running' | 'stale' | 'paused' | 'unknown' = 'running';
  await page.route('**/api/chats/*/index', async route => {
    const base = await (await route.fetch()).json();
    const estimate = { lower_seconds: mode === 'unknown' ? 0 : 600,
      upper_seconds: mode === 'unknown' ? 0 : 900, eta_samples: 12,
      measuring: mode === 'stale', projected: false, stale: mode === 'stale', paused: mode === 'paused' };
    await route.fulfill({ json: { ...base,
      semantic: { ...base.semantic, enabled: 1, profile: 'small', preparation_state: 'ready',
        paused: Number(mode === 'paused'), ready_segments: 2, total_segments: 20,
        works: [{ state: 'pending', count: 18, chunks_total: 100, chunks_done: 20 }],
        estimate, estimated_remaining_seconds: mode === 'running' ? 750 : null },
      media: { ...base.media, images_enabled: 1, ocr_enabled: 1, visual_profile: 'clip',
        preparation_state: 'ready', paused: Number(mode === 'paused'), ocr_paused: Number(mode === 'paused'),
        ocr_dense_paused: Number(mode === 'paused'), total_photos: 100, images_ready: 10,
        ocr_ready: 20, ocr_failed: 0, ocr_nonempty_ready: 10, ocr_dense_ready: 5,
        ocr_dense_available: true, images_estimated_remaining_seconds: 750,
        ocr_estimated_remaining_seconds: 750, ocr_dense_estimated_remaining_seconds: 750,
        images_estimate: estimate, ocr_estimate: estimate, ocr_dense_estimate: estimate,
        preparation_estimated_remaining_seconds: null,
        preparation_estimate: { ...estimate, upper_seconds: mode === 'unknown' ? 0 : 1800 },
        queues: { ocr: { pending: 80, units_per_minute: 20 }, clip: { pending: 90, units_per_minute: 20 } },
      },
    } });
  });
  await page.goto('/');
  await page.getByRole('button', { name: /^Настройки индексации / }).first().click();
  const text = page.getByRole('region', { name: 'Текст · E5' });
  await expect(text.locator('.index-eta')).toHaveText('Осталось примерно 10–15 мин.');
  await page.getByText('Скорость и очереди подготовки', { exact: true }).click();
  await expect(page.getByText('Вся подготовка: Осталось примерно 10–30 мин.')).toBeVisible();
  mode = 'unknown';
  await expect(text.locator('.index-eta')).toHaveText('Оценка появится после первых батчей.');
  await expect(page.getByText('Вся подготовка: Собираем статистику для оценки времени.')).toBeVisible();
  mode = 'stale';
  await expect(text.locator('.index-eta')).toHaveText('Оценка уточняется: давно не было завершённых батчей.');
  mode = 'paused';
  await expect(text.locator('.index-eta')).toHaveText('Продолжите индексацию для оценки времени.');
  await expect(page.getByText('Вся подготовка: Продолжите индексацию для оценки времени.')).toBeVisible();
  await page.getByRole('slider').press('End');
  await expect(page.getByText('All preparation: Resume indexing to estimate the remaining time.')).toBeVisible();
  mode = 'running';
  const english = page.getByRole('region', { name: 'Text · E5' });
  await expect(english.locator('.index-eta')).toHaveText('About 10–15 min remaining.');
});

for (const paused of [false, true]) {
  test(`failed E5 queue shows its error and retries instead of displaying a stale ETA (${paused})`, async ({ page }) => {
    let retried = false;
    const semantic = {
      runtime_installed: true, enabled: 1, dense_available: true, profile: 'small',
      preparation_state: 'ready', paused: Number(paused), batch_size: 16,
      ready_segments: 1317, total_segments: 2040, pending_segments: 723,
      error: null, index_error: '<img src=x onerror=window.indexInjected=true> GPU error',
      estimated_remaining_seconds: 97719.2,
      works: [{ state: 'failed', count: paused ? 1 : 723, chunks_total: 128, chunks_done: 0 },
        ...(paused ? [{ state: 'pending', count: 722, chunks_total: 128, chunks_done: 0 }] : [])],
    };
    await page.route('**/api/chats/*/index', async route => {
      const value = await (await route.fetch()).json();
      await route.fulfill({ json: { ...value, semantic: retried ? { ...semantic,
        paused: 0, index_error: null, works: [{ state: 'pending', count: 723, chunks_total: 128, chunks_done: 0 }],
      } : semantic } });
    });
    await page.route('**/api/chats/*/index/text/retry', async route => {
      retried = true;
      const value = await (await page.request.get(route.request().url().replace('/text/retry', ''))).json();
      await route.fulfill({ json: { ...value, semantic: { ...semantic, paused: 0, index_error: null,
        works: [{ state: 'pending', count: 723, chunks_total: 128, chunks_done: 0 }],
      } } });
    });
    await page.goto('/');
    await page.getByRole('button', { name: /^Настройки индексации / }).first().click();
    const card = page.getByRole('region', { name: 'Текст · E5' });
    await expect(card).toContainText('Ошибка индексации');
    await expect(card).toContainText(`Задач с ошибкой: ${paused ? 1 : 723}`);
    await expect(card.getByRole('alert')).toContainText('GPU error');
    await expect(card.locator('.index-eta')).toHaveCount(0);
    expect(await page.evaluate(() => (window as unknown as Record<string, unknown>).indexInjected)).toBeUndefined();
    await page.getByRole('slider').press('End');
    const english = page.getByRole('region', { name: 'Text · E5' });
    await expect(english).toContainText('Indexing error');
    await english.getByRole('button', { name: `Retry errors (${paused ? 1 : 723})`, exact: true }).click();
    await expect.poll(() => retried).toBe(true);
    await expect(english).toContainText('1317 / 2040');
    await expect(english.locator('.index-errors')).toHaveCount(0);
    await expect(english.getByRole('button', { name: 'Pause indexing', exact: true })).toBeVisible();
  });
}

async function mockIndex(page: import('@playwright/test').Page, ocrOnly = false) {
  await page.route('**/api/chats/*/index', async route => {
    const response = await route.fetch();
    const value = await response.json();
    await route.fulfill({ json: {
      semantic: { ...value.semantic, runtime_installed: true, enabled: 1, profile: 'small',
        paused: 1, batch_size: 4, ready_segments: 2, total_segments: 20,
        estimated_remaining_seconds: 7200 },
      media: { ...value.media, images_enabled: ocrOnly ? 0 : 1, ocr_enabled: 1,
        ocr_runtime_installed: true, paused: 1, ocr_paused: 1, batch_size: 1,
        ocr_ready: 20, ocr_failed: 5, ocr_nonempty_ready: 15, ocr_dense_ready: 8,
        ocr_dense_available: true, ocr_estimated_remaining_seconds: 1800,
        images_ready: 10, total_photos: 100, images_estimated_remaining_seconds: 600 },
    } });
  });
}

test('CLIP shows a counted retry action at progress and preserves ready images', async ({ page }) => {
  let retried = false;
  const clip = { images_enabled: 1, paused: 0, images_ready: 97, images_failed: 3,
    total_photos: 100, images_estimated_remaining_seconds: 0 };
  await page.route('**/api/chats/*/index', async route => {
    const base = await (await route.fetch()).json();
    await route.fulfill({ json: { ...base, media: { ...base.media, ...clip,
      images_failed: retried ? 0 : 3 } } });
  });
  await page.route('**/api/chats/*/index/images/retry', async route => {
    retried = true;
    const base = await (await page.request.get(route.request().url().replace('/images/retry', ''))).json();
    await route.fulfill({ json: { ...base, media: { ...base.media, ...clip, images_failed: 0 } } });
  });
  await page.goto('/');
  await page.getByRole('button', { name: /^Настройки индексации / }).first().click();
  const card = page.getByRole('region', { name: 'Изображения · CLIP' });
  await expect(card).toContainText('Ошибка индексации');
  await expect(card).toContainText('Задач с ошибкой: 3');
  await expect(card).toContainText('готовые результаты сохранятся');
  await expect(card.locator('.index-eta')).toHaveCount(0);
  await card.getByRole('button', { name: 'Повторить ошибки (3)', exact: true }).click();
  await expect.poll(() => retried).toBe(true);
  await expect(card).toContainText('97 / 100');
  await expect(card.locator('.index-errors')).toHaveCount(0);
});

for (const recognitionFailed of [0, 5]) {
  test(`OCR retries semantic errors independently of recognition (${recognitionFailed})`, async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.emulateMedia({ colorScheme: 'dark' });
    let retried = false;
    const ocr = { ocr_enabled: 1, ocr_paused: 1, total_photos: 100,
      ocr_ready: 100 - recognitionFailed, ocr_failed: recognitionFailed, ocr_dense_failed: 2,
      ocr_dense_paused: 1, ocr_nonempty_ready: 20, ocr_dense_ready: 18, ocr_dense_available: true };
    await page.route('**/api/chats/*/index', async route => {
      const base = await (await route.fetch()).json();
      await route.fulfill({ json: { ...base, media: { ...base.media, ...ocr,
        ...(retried ? { ocr_dense_failed: 0, ocr_dense_paused: 0 } : {}) } } });
    });
    let release!: () => void;
    await page.route('**/api/chats/*/index/ocr_dense/retry', async route => {
      const base = await (await page.request.get(route.request().url().replace('/ocr_dense/retry', ''))).json();
      await new Promise<void>(resolve => { release = resolve; });
      retried = true;
      await route.fulfill({ json: { ...base, media: { ...base.media, ...ocr,
        ocr_dense_failed: 0, ocr_dense_paused: 0 } } });
    });
    await page.goto('/');
    await page.getByRole('button', { name: /^Настройки индексации / }).first().click();
    const card = page.getByRole('region', { name: 'Текст на изображениях · OCR' });
    await expect(card).toContainText('Обработано: 100 / 100');
    await expect(card).toContainText(recognitionFailed ? 'Требуется повтор' : 'Готово');
    await expect(card).toContainText('Смысловой OCR с ошибкой: 2');
    const retry = card.locator('.ocr-semantic-progress').getByRole('button', { name: 'Повторить ошибки (2)', exact: true });
    await retry.scrollIntoViewIfNeeded();
    const box = await retry.boundingBox();
    expect(box!.x).toBeGreaterThanOrEqual(0);
    expect(box!.x + box!.width).toBeLessThanOrEqual(390);
    await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
    await retry.click();
    await expect.poll(() => !!release).toBe(true);
    await expect(retry).toBeDisabled();
    await expect(card.getByRole('button', { name: 'Продолжить OCR', exact: true })).toBeDisabled();
    release();
    await expect(card.locator('.ocr-semantic-progress .index-errors')).toHaveCount(0);
    await expect(card.locator('.index-errors')).toHaveCount(recognitionFailed ? 1 : 0);
    await expect(card).toContainText('OCR по смыслу: 18 / 20 с текстом');
    await expect(card).toContainText('Обработано: 100 / 100');
    await expect(card.getByRole('button', { name: 'Продолжить OCR', exact: true })).toBeVisible();
    await expect(card.getByRole('button', { name: 'Пауза смыслового OCR', exact: true })).toBeVisible();
  });
}

test('matching text and image cards offer large chat-scoped batches without a paused ETA', async ({ page }) => {
  await mockIndex(page);
  await page.goto('/');
  await expect(page.getByLabel('Режим поиска')).toHaveValue('hybrid');
  await page.getByRole('button', { name: /^Настройки индексации / }).first().click();
  const text = page.getByRole('region', { name: 'Текст · E5' });
  const images = page.getByRole('region', { name: 'Изображения · CLIP' });
  await expect(text).toContainText('Продолжите индексацию для оценки времени.');
  await expect(images).toContainText('Продолжите индексацию для оценки времени.');
  for (const [card, max] of [[text, '128'], [images, '32']] as const) {
    await expect(card.getByRole('spinbutton')).toHaveAttribute('max', max);
    await expect(card.locator('.batch-presets button')).toHaveCount(6);
    await expect(card.getByRole('button', { name: 'Продолжить индексацию' })).toBeVisible();
  }
  const dimensions = await page.locator('.index-batch input').evaluateAll(inputs => inputs.map(input => {
    const style = getComputedStyle(input); return [style.width, style.padding, style.marginTop, style.borderColor, style.borderRadius, style.fontSize];
  }));
  expect(dimensions[0]).toEqual(dimensions[1]);
  await page.getByRole('slider').press('End');
  await expect(page.getByRole('region', { name: 'Text · E5' })).toContainText('Resume indexing to estimate the remaining time.');
  await expect(page.getByRole('region', { name: 'Images · CLIP' }).getByRole('spinbutton')).toHaveAccessibleName('Batch size: Images');
});

test('pending batch save disables preparation and other chat-index mutations', async ({ page }) => {
  await mockIndex(page);
  let release!: () => void;
  await page.route('**/api/chats/*/index/settings', async route => {
    expect(route.request().postDataJSON()).toEqual({ embedding_batch: 64 });
    const response = await route.fetch();
    await new Promise<void>(resolve => { release = resolve; });
    await route.fulfill({ response });
  });
  await page.goto('/');
  await page.getByRole('button', { name: /^Настройки индексации / }).first().click();
  const text = page.getByRole('region', { name: 'Текст · E5' });
  await text.getByRole('button', { name: '64', exact: true }).click();
  await text.getByRole('button', { name: 'Применить батч' }).click();
  await expect.poll(() => !!release).toBe(true);
  await expect(page.getByLabel('Модель смыслового поиска')).toHaveCount(0);
  await expect(text.getByRole('button', { name: 'Продолжить индексацию' })).toBeDisabled();
  await expect(page.getByRole('region', { name: 'Изображения · CLIP' }).getByRole('spinbutton')).toBeDisabled();
  release();
  await expect(text.getByRole('spinbutton')).toHaveValue('64');
});

test('OCR can pause and resume before CLIP has been prepared', async ({ page }) => {
  await mockIndex(page, true);
  let action = '';
  await page.route('**/api/chats/*/index/ocr/resume', async route => {
    action = 'resume';
    const base = await (await page.request.get(route.request().url().replace('/ocr/resume', ''))).json();
    await route.fulfill({ json: { ...base, media: { ...base.media, ocr_enabled: 1, images_enabled: 0, ocr_paused: 0 } } });
  });
  await page.goto('/');
  await page.getByRole('button', { name: /^Настройки индексации / }).first().click();
  await page.getByRole('button', { name: 'Продолжить OCR', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Пауза OCR', exact: true })).toBeVisible();
  expect(action).toBe('resume');
});

test('OCR batches validate integers and persist per chat without resuming recognition', async ({ page }) => {
  const settings = { ocr_batch_size: 1, ocr_region_batch_size: 8 };
  let saves = 0;
  const media = { ocr_enabled: 1, ocr_paused: 1, total_photos: 100, ocr_ready: 10,
    ocr_failed: 0, ocr_dense_ready: 0, ocr_nonempty_ready: 0, ocr_dense_available: true,
    ocr_dense_paused: 1, photo_attachments: 120, photo_messages: 110,
    queues: { ocr: { pending: 90, units_per_minute: 30 } } };
  await page.route('**/api/chats/*/index', async route => {
    const base = await (await route.fetch()).json();
    await route.fulfill({ json: { ...base, media: { ...base.media, ...media, ...settings } } });
  });
  await page.route('**/api/chats/*/index/settings', async route => {
    expect(route.request().postDataJSON()).toEqual({ ocr_batch_size: 2, ocr_region_batch_size: 16 });
    Object.assign(settings, route.request().postDataJSON());
    saves += 1;
    const base = await (await page.request.get(route.request().url().replace('/settings', ''))).json();
    await route.fulfill({ json: { ...base, media: { ...base.media, ...media, ...settings } } });
  });
  await page.goto('/');
  const open = page.getByRole('button', { name: /^Настройки индексации / }).first();
  await open.click();
  const card = page.getByRole('region', { name: 'Текст на изображениях · OCR' });
  await card.getByText('Батчи OCR', { exact: true }).click();
  const images = card.getByRole('spinbutton', { name: 'Изображений в батче OCR', exact: true });
  const regions = card.getByRole('spinbutton', { name: 'Областей текста в батче OCR', exact: true });
  for (const invalid of ['0', '5', '1.5']) {
    await images.fill(invalid);
    await expect(card.getByRole('button', { name: 'Применить батчи OCR' })).toBeDisabled();
  }
  await images.fill('2');
  await regions.fill('33');
  await expect(card.getByRole('button', { name: 'Применить батчи OCR' })).toBeDisabled();
  await regions.fill('16');
  await card.getByRole('button', { name: 'Применить батчи OCR' }).click();
  await expect.poll(() => saves).toBe(1);
  await expect(card.getByRole('button', { name: 'Применить батчи OCR' })).toHaveCount(0);
  await expect(card.getByRole('button', { name: 'Продолжить OCR', exact: true })).toBeVisible();
  await page.keyboard.press('Escape');
  await open.click();
  await card.getByText('Батчи OCR', { exact: true }).click();
  await expect(images).toHaveValue('2');
  await expect(regions).toHaveValue('16');
  await page.getByRole('slider').press('End');
  await expect(page.getByRole('spinbutton', { name: 'Images per OCR batch', exact: true })).toHaveValue('2');
  await expect(page.getByRole('region', { name: 'Text in images · OCR' })).toContainText('Waiting for newly recognized images.');
});

test('recognition and semantic OCR pause independently', async ({ page }) => {
  const state = { ocr_enabled: 1, ocr_paused: 1, ocr_dense_paused: 1,
    total_photos: 100, ocr_ready: 10, ocr_failed: 0, ocr_nonempty_ready: 5,
    ocr_dense_ready: 3, ocr_dense_available: true };
  await page.route('**/api/chats/*/index', async route => {
    const base = await (await route.fetch()).json();
    await route.fulfill({ json: { ...base, media: { ...base.media, ...state } } });
  });
  await page.route(/\/api\/chats\/[^/]+\/index\/(ocr|ocr_dense)\/(pause|resume)$/, async route => {
    const parts = route.request().url().split('/');
    const kind = parts.at(-2);
    state[kind === 'ocr' ? 'ocr_paused' : 'ocr_dense_paused'] = Number(parts.at(-1) === 'pause');
    const base = await (await page.request.get(parts.slice(0, -2).join('/'))).json();
    await route.fulfill({ json: { ...base, media: { ...base.media, ...state } } });
  });
  await page.goto('/');
  await page.getByRole('button', { name: /^Настройки индексации / }).first().click();
  const card = page.getByRole('region', { name: 'Текст на изображениях · OCR' });
  await card.getByRole('button', { name: 'Продолжить смысловой OCR', exact: true }).click();
  await expect(card.getByRole('button', { name: 'Пауза смыслового OCR', exact: true })).toBeVisible();
  await expect(card.getByRole('button', { name: 'Продолжить OCR', exact: true })).toBeVisible();
  await card.getByRole('button', { name: 'Продолжить OCR', exact: true }).click();
  await expect(card.getByRole('button', { name: 'Пауза OCR', exact: true })).toBeVisible();
  await card.getByRole('button', { name: 'Пауза смыслового OCR', exact: true }).click();
  await expect(card.getByRole('button', { name: 'Продолжить смысловой OCR', exact: true })).toBeVisible();
  await expect(card.getByRole('button', { name: 'Пауза OCR', exact: true })).toBeVisible();
});

test('late OCR batch save cannot overwrite edits in a reopened dialog', async ({ page }) => {
  await mockIndex(page, true);
  let release!: () => void;
  await page.route('**/api/chats/*/index/settings', async route => {
    const base = await (await page.request.get(route.request().url().replace('/settings', ''))).json();
    await new Promise<void>(resolve => { release = resolve; });
    await route.fulfill({ json: { ...base, media: { ...base.media, ocr_batch_size: 4 } } });
  });
  await page.goto('/');
  const open = page.getByRole('button', { name: /^Настройки индексации / }).first();
  await open.click();
  const card = page.getByRole('region', { name: 'Текст на изображениях · OCR' });
  await card.getByText('Батчи OCR', { exact: true }).click();
  const images = card.getByRole('spinbutton', { name: 'Изображений в батче OCR', exact: true });
  await images.fill('4');
  await card.getByRole('button', { name: 'Применить батчи OCR' }).click();
  await expect.poll(() => !!release).toBe(true);
  await page.keyboard.press('Escape');
  await open.click();
  await card.getByText('Батчи OCR', { exact: true }).click();
  await images.fill('2');
  const response = page.waitForResponse(value => value.request().method() === 'PATCH' && value.url().endsWith('/index/settings'));
  release();
  await (await response).finished();
  // Allow the settled fetch callback and React effects to run before checking stale edits.
  await page.evaluate(() => new Promise<void>(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
  await expect(images).toHaveValue('2');
  await expect(card.getByRole('button', { name: 'Применить батчи OCR' })).toBeEnabled();
});

test('OCR recognition and semantic progress are visible with chat-scoped ETA and errors', async ({ page }) => {
  await mockIndex(page);
  await page.goto('/');
  await page.getByRole('button', { name: /^Настройки индексации / }).first().click();
  const ocr = page.getByRole('region', { name: 'Текст на изображениях · OCR' });
  await expect(ocr).toBeVisible();
  await expect(ocr).toContainText('Обработано: 25 / 100');
  await expect(ocr).toContainText('На паузе');
  await expect(ocr).toContainText('Продолжите индексацию для оценки времени.');
  await expect(ocr).toContainText('OCR с ошибкой: 5');
  await expect(ocr.getByRole('progressbar', { name: 'Прогресс распознавания OCR', exact: true })).toHaveAttribute('value', '25');
  await expect(ocr.getByRole('progressbar', { name: 'Прогресс распознавания OCR', exact: true })).toHaveAttribute('max', '100');
  await expect(ocr.getByRole('progressbar', { name: 'Прогресс смысловой индексации OCR', exact: true })).toHaveAttribute('value', '8');
  await expect(ocr.getByRole('progressbar', { name: 'Прогресс смысловой индексации OCR', exact: true })).toHaveAttribute('max', '15');
  await page.getByRole('slider').press('End');
  const english = page.getByRole('region', { name: 'Text in images · OCR' });
  await expect(english).toContainText('Processed: 25 / 100');
  await expect(english).toContainText('Resume indexing to estimate the remaining time.');
  await expect(english).toContainText('Semantic OCR: 8 / 15 containing text');
});

for (const done of [false, true]) {
  test(`OCR ${done ? 'completed recognition keeps unfinished semantic progress visible' : 'unknown ETA avoids a fabricated remaining time'}`, async ({ page }) => {
    await page.route('**/api/chats/*/index', async route => {
      const response = await route.fetch();
      const value = await response.json();
      await route.fulfill({ json: { ...value, media: { ...value.media,
        ocr_enabled: 1, images_enabled: 0, paused: 0, ocr_paused: 0, ocr_dense_paused: 0, total_photos: 100,
        ocr_estimate: { lower_seconds: null, upper_seconds: null },
        ocr_ready: done ? 100 : 0, ocr_failed: 0, ocr_dense_ready: 8,
        ocr_nonempty_ready: 15, ocr_dense_available: true,
        ocr_estimated_remaining_seconds: done ? 0 : null,
      } } });
    });
    await page.goto('/');
    await page.getByRole('button', { name: /^Настройки индексации / }).first().click();
    const ocr = page.getByRole('region', { name: 'Текст на изображениях · OCR' });
    await expect(ocr).toContainText(done ? 'Распознавание завершено.' : 'Оценка появится после первых изображений.');
    await expect(ocr).toContainText('OCR по смыслу: 8 / 15 с текстом');
  });
}
