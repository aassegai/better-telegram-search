import { expect, test } from '@playwright/test';

async function mockIndex(page: import('@playwright/test').Page, ocrOnly = false) {
  await page.route('**/api/chats/*/index', async route => {
    const response = await route.fetch();
    const value = await response.json();
    await route.fulfill({ json: {
      semantic: { ...value.semantic, runtime_installed: true, enabled: 1, profile: 'small',
        paused: 1, batch_size: 4, ready_segments: 2, total_segments: 20,
        estimated_remaining_seconds: 7200 },
      media: { ...value.media, images_enabled: ocrOnly ? 0 : 1, ocr_enabled: 1,
        ocr_runtime_installed: true, paused: 1, batch_size: 1,
        images_ready: 10, total_photos: 100, images_estimated_remaining_seconds: 600 },
    } });
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
  await page.route('**/api/chats/*/index/media/resume', async route => {
    action = 'resume';
    const base = await (await page.request.get(route.request().url().replace('/media/resume', ''))).json();
    await route.fulfill({ json: { ...base, media: { ...base.media, ocr_enabled: 1, images_enabled: 0, paused: 0 } } });
  });
  await page.goto('/');
  await page.getByRole('button', { name: /^Настройки индексации / }).first().click();
  await page.getByText('Текст на изображениях · OCR', { exact: true }).click();
  await page.getByRole('button', { name: 'Продолжить OCR', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Пауза OCR', exact: true })).toBeVisible();
  expect(action).toBe('resume');
});
