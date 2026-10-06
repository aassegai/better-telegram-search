import { test, expect } from '@playwright/test';

const status = {
  runtime_installed: true, dense_available: true, enabled: 1, paused: 0,
  preparation_state: 'ready', profile: 'small', error: null,
  download_completed_bytes: 0, download_total_bytes: 0,
  total_segments: 3, ready_segments: 1, pending_segments: 2,
  works: [{ state: 'pending', count: 2, chunks_total: 12, chunks_done: 4 }],
  profiles: [
    { profile: 'small', model_id: 'synthetic/small', download_bytes: 487850043, dimension: 384 },
    { profile: 'base', model_id: 'synthetic/base', download_bytes: 1127322481, dimension: 768 },
  ],
};

test('search modes show partial coverage, fallback and exact phrase semantics', async ({ page }) => {
  await page.route('**/api/semantic', route => route.fulfill({ json: status }));
  await page.goto('/');
  await page.getByRole('checkbox', { name: 'Изображения', exact: true }).uncheck();
  await page.getByRole('checkbox', { name: 'OCR', exact: true }).uncheck();
  await expect(page.getByText('Смысловой индекс: 1 / 3 сегментов')).toBeVisible();
  await page.getByLabel('Режим поиска').selectOption('hybrid');
  await page.getByLabel('Поисковый запрос').fill('велосипед');
  let requested = '';
  await page.route('**/api/search?**', async route => {
    requested = new URL(route.request().url()).searchParams.get('mode') || '';
    const response = await route.fetch();
    await route.fulfill({ response });
  });
  await page.getByRole('button', { name: 'Найти' }).click();
  await expect(page.getByText('Смысловой поиск ещё не готов. Показаны результаты по словам.')).toBeVisible();
  expect(requested).toBe('hybrid');
  await expect(page.locator('.results-heading')).toContainText('По словам');
  await page.getByLabel('Точная фраза').check();
  await expect(page.getByLabel('Режим поиска')).toBeDisabled();
  await page.getByRole('button', { name: 'Найти' }).click();
  await expect(page.getByText('Смысловой поиск ещё не готов. Показаны результаты по словам.')).toHaveCount(0);
  await expect(page.getByText('Точная фраза ищется в сообщениях и распознанном тексте фотографий.')).toBeVisible();
});

test('chunk result renders the eighth message and escapes its text', async ({ page }) => {
  await page.route('**/api/search?**', route => route.fulfill({ json: {
    backend: 'e5_chunks_hybrid', effective_mode: 'hybrid', warnings: [], has_more: false,
    results: [{ chat_id: 'synthetic', chat_name: 'Синтетический разговор', message_id: 1,
      chunk_id: 'synthetic-chunk', timestamp: 1750000000, matched_by: ['words', 'meaning'],
      matched_parts: Array.from({ length: 8 }, (_, i) => ({ message_id: i + 1, char_start: 0, char_end: 20 })),
      messages: Array.from({ length: 8 }, (_, i) => ({ chat_id: 'synthetic', message_id: i + 1,
        timestamp: 1750000000 + i, author: 'Синтетический автор', kind: 'message', media: [],
        matches_filters: true, text: i === 7 ? '<img src=x onerror=window.chunkInjected=true> уникальный ремонт' : 'обычная реплика',
        action: null, reply_to: null, forwarded_from: null, edited_timestamp: null })),
    }],
  } }));
  await page.goto('/');
  await page.getByLabel('Режим поиска').selectOption('hybrid');
  await page.getByLabel('Поисковый запрос').fill('ремонт');
  await page.getByRole('button', { name: 'Найти' }).click();
  await expect(page.locator('.result-card .message')).toHaveCount(8);
  await expect(page.locator('.result-card')).toContainText('уникальный ремонт');
  await expect(page.locator('.result-card')).toContainText('Близкий смысл');
  expect(await page.evaluate(() => (window as unknown as Record<string, unknown>).chunkInjected)).toBeUndefined();
});

test('model change requires explicit reindex and late preparation preserves a reopened dialog', async ({ page }) => {
  await page.route('**/api/semantic', route => route.fulfill({ json: status }));
  await page.goto('/');
  await page.getByRole('button', { name: 'Настройки и диагностика' }).click();
  await page.getByLabel('Модель смыслового поиска').selectOption('base');
  await expect(page.getByRole('button', { name: 'Подготовить модель и индекс' })).toBeDisabled();
  await page.getByLabel('Разрешить полную переиндексацию').check();
  await page.getByLabel('Использовать только локальный кэш').check();
  let release: () => void = () => {};
  let received: () => void = () => {};
  const waiting = new Promise<void>(resolve => { release = resolve; });
  const started = new Promise<void>(resolve => { received = resolve; });
  await page.route('**/api/semantic/prepare', async route => {
    expect(route.request().postDataJSON()).toMatchObject({ profile: 'base', reindex: true, offline: true });
    received(); await waiting;
    await route.fulfill({ json: { ...status, preparation_state: 'downloading' } });
  });
  await page.getByRole('button', { name: 'Подготовить модель и индекс' }).click();
  await started;
  await expect(page.getByRole('button', { name: 'Пауза индексирования' })).toBeDisabled();
  await page.keyboard.press('Escape');
  await page.getByRole('button', { name: 'Настройки и диагностика' }).click();
  const completed = page.waitForResponse(response => response.url().endsWith('/semantic/prepare'));
  release(); await completed;
  await expect(page.getByRole('heading', { name: 'Настройки и диагностика' })).toBeVisible();
  await expect(page.getByLabel('Модель смыслового поиска')).toHaveValue('small');
  await expect(page.getByLabel('Модель смыслового поиска')).toBeEnabled();
  await expect(page.getByRole('button', { name: 'Подготовить модель и индекс' })).toBeEnabled();
});
