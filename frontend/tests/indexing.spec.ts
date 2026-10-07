import { expect, test } from '@playwright/test';

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
  test(`OCR shows recognition and semantic failures and retries both (${recognitionFailed})`, async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.emulateMedia({ colorScheme: 'dark' });
    let retried = false;
    const ocr = { ocr_enabled: 1, ocr_paused: 1, total_photos: 100,
      ocr_ready: 100 - recognitionFailed, ocr_failed: recognitionFailed, ocr_dense_failed: 2,
      ocr_nonempty_ready: 20, ocr_dense_ready: 18, ocr_dense_available: true };
    await page.route('**/api/chats/*/index', async route => {
      const base = await (await route.fetch()).json();
      await route.fulfill({ json: { ...base, media: { ...base.media, ...ocr,
        ...(retried ? { ocr_failed: 0, ocr_dense_failed: 0, ocr_paused: 0 } : {}) } } });
    });
    let release!: () => void;
    await page.route('**/api/chats/*/index/ocr/retry', async route => {
      const base = await (await page.request.get(route.request().url().replace('/ocr/retry', ''))).json();
      await new Promise<void>(resolve => { release = resolve; });
      retried = true;
      await route.fulfill({ json: { ...base, media: { ...base.media, ...ocr,
        ocr_failed: 0, ocr_dense_failed: 0, ocr_paused: 0 } } });
    });
    await page.goto('/');
    await page.getByRole('button', { name: /^Настройки индексации / }).first().click();
    const card = page.getByRole('region', { name: 'Текст на изображениях · OCR' });
    await expect(card).toContainText('Обработано: 100 / 100');
    await expect(card).toContainText('Требуется повтор');
    await expect(card).toContainText('Смысловой OCR с ошибкой: 2');
    const retry = card.getByRole('button', { name: `Повторить ошибки (${recognitionFailed + 2})`, exact: true });
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
    await expect(card.locator('.index-errors')).toHaveCount(0);
    await expect(card).toContainText('OCR по смыслу: 18 / 20 с текстом');
    await expect(card).toContainText(`Обработано: ${100 - recognitionFailed} / 100`);
    await expect(card.getByRole('button', { name: 'Пауза OCR', exact: true })).toBeVisible();
  });
}

test('matching text and image cards offer large chat-scoped batches and whole-queue ETA', async ({ page }) => {
  await mockIndex(page);
  await page.goto('/');
  await expect(page.getByLabel('Режим поиска')).toHaveValue('hybrid');
  await page.getByRole('button', { name: /^Настройки индексации / }).first().click();
  const text = page.getByRole('region', { name: 'Текст · E5' });
  const images = page.getByRole('region', { name: 'Изображения · CLIP' });
  await expect(text).toContainText('Осталось примерно 2 ч 0 мин.');
  await expect(images).toContainText('Осталось примерно 10 мин.');
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
  await expect(page.getByRole('region', { name: 'Text · E5' })).toContainText('About 2 h 0 min remaining.');
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

test('OCR recognition and semantic progress are visible with chat-scoped ETA and errors', async ({ page }) => {
  await mockIndex(page);
  await page.goto('/');
  await page.getByRole('button', { name: /^Настройки индексации / }).first().click();
  const ocr = page.getByRole('region', { name: 'Текст на изображениях · OCR' });
  await expect(ocr).toBeVisible();
  await expect(ocr).toContainText('Обработано: 25 / 100');
  await expect(ocr).toContainText('На паузе');
  await expect(ocr).toContainText('Осталось примерно 30 мин.');
  await expect(ocr).toContainText('OCR с ошибкой: 5');
  await expect(ocr.getByRole('progressbar', { name: 'Прогресс распознавания OCR', exact: true })).toHaveAttribute('value', '25');
  await expect(ocr.getByRole('progressbar', { name: 'Прогресс распознавания OCR', exact: true })).toHaveAttribute('max', '100');
  await expect(ocr.getByRole('progressbar', { name: 'Прогресс смысловой индексации OCR', exact: true })).toHaveAttribute('value', '8');
  await expect(ocr.getByRole('progressbar', { name: 'Прогресс смысловой индексации OCR', exact: true })).toHaveAttribute('max', '15');
  await page.getByRole('slider').press('End');
  const english = page.getByRole('region', { name: 'Text in images · OCR' });
  await expect(english).toContainText('Processed: 25 / 100');
  await expect(english).toContainText('About 30 min remaining.');
  await expect(english).toContainText('Semantic OCR: 8 / 15 containing text');
});

for (const done of [false, true]) {
  test(`OCR ${done ? 'completed recognition keeps unfinished semantic progress visible' : 'unknown ETA avoids a fabricated remaining time'}`, async ({ page }) => {
    await page.route('**/api/chats/*/index', async route => {
      const response = await route.fetch();
      const value = await response.json();
      await route.fulfill({ json: { ...value, media: { ...value.media,
        ocr_enabled: 1, images_enabled: 0, paused: 0, total_photos: 100,
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
