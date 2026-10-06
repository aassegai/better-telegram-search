import { test, expect } from '@playwright/test';

const media = { ocr_enabled: 1, images_enabled: 1, paused: 0, running: false, preparation_state: 'ready', error: null,
  resource_error: null, total_photos: 2, ocr_ready: 1, ocr_failed: 0, ocr_dense_ready: 1, images_ready: 2,
  missing_refs: 1, ocr_available: true, images_available: true, ocr_runtime_installed: true, device: 'cpu' };
const hit = { chat_id: 'synthetic', chat_name: 'Синтетическая фотография', message_id: 1, timestamp: 1750000000,
  media_id: 999, ocr_text: '<img src=x onerror=window.ocrInjected=true> Заказ 123456', ocr_confidence: 91,
  matched_by: ['ocr_words'], result_type: 'ocr', messages: [{ chat_id: 'synthetic', message_id: 1, timestamp: 1750000000,
    author: 'Автор', text: 'Подпись фото', kind: 'message', action: null, reply_to: null, forwarded_from: null,
    matches_filters: true, media: [{ id: 999, kind: 'photo', status: 'ready' }], edited_timestamp: null }] };

test('OCR evidence stays escaped and tabs clear the previous result', async ({ page }) => {
  await page.route('**/api/media-index', route => route.fulfill({ json: media }));
  await page.route('**/api/media/999', route => route.fulfill({ status: 404 }));
  await page.route('**/api/search?**', route => route.fulfill({ json: { results: [hit], effective_mode: 'words', warnings: [], has_more: false } }));
  await page.goto('/');
  await expect(page.getByText(/Фотографии: 2 \/ 2 · OCR: 1 \/ 2/)).toBeVisible();
  await page.getByRole('tab', { name: 'OCR', exact: true }).click();
  await page.getByLabel('Поисковый запрос').fill('123456');
  await page.getByRole('button', { name: 'Найти' }).click();
  await expect(page.locator('.match-reasons')).toContainText('Слова на фотографии');
  await page.getByText('Распознанный текст · уверенность OCR 91 / 100').click();
  await expect(page.locator('.ocr-evidence')).toContainText('<img src=x onerror=window.ocrInjected=true>');
  expect(await page.evaluate(() => (window as unknown as Record<string, unknown>).ocrInjected)).toBeUndefined();
  await expect(page.getByText('Изображение недоступно в папке источника')).toBeVisible();
  await page.getByRole('tab', { name: 'Изображения', exact: true }).click();
  await expect(page.locator('.result-card')).toHaveCount(0);
  await page.getByRole('button', { name: 'Найти' }).click();
  await expect(page.locator('.photo-grid .result-card')).toHaveCount(1);
});

test('resource settings persist and closing the panel ignores late media preparation', async ({ page }) => {
  await page.route('**/api/media-index', route => route.fulfill({ json: media }));
  await page.goto('/');
  await page.getByRole('button', { name: '⚙ Настройки и диагностика' }).click();
  await page.getByLabel('Потоков CPU', { exact: true }).fill('2');
  await page.getByRole('button', { name: 'Сохранить ресурсы' }).click();
  await expect(page.getByText(/Настройки сохранены/)).toBeVisible();
  let release!: () => void;
  await page.route('**/api/media-index/prepare', async route => {
    await new Promise<void>(resolve => { release = resolve; });
    await route.fulfill({ json: { ...media, preparation_state: 'preparing' } });
  });
  await page.getByRole('button', { name: 'Подготовить OCR', exact: true }).click();
  await expect.poll(() => !!release).toBe(true);
  await page.getByRole('button', { name: 'Закрыть', exact: true }).click();
  await page.getByRole('button', { name: '⚙ Настройки и диагностика' }).click();
  const response = page.waitForResponse('**/api/media-index/prepare');
  release();
  await response;
  await expect(page.getByRole('dialog')).toBeVisible();
  await expect(page.getByLabel('Потоков CPU', { exact: true })).toHaveValue('2');
  await expect(page.getByRole('button', { name: 'Подготовить OCR', exact: true })).toBeEnabled();
});
