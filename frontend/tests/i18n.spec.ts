import { test, expect } from '@playwright/test';
import path from 'node:path';
import fs from 'node:fs';
import ts from 'typescript';
import { setLanguage, t } from '../src/i18n';

test('translation catalogs cover interface strings and preserve placeholder names', () => {
  const catalog = { ...JSON.parse(fs.readFileSync('src/locales/en.json', 'utf8')),
    ...JSON.parse(fs.readFileSync('src/locales/backend.en.json', 'utf8')) } as Record<string, string>;
  for (const [key, value] of Object.entries(catalog)) {
    expect(value.trim(), key).not.toBe('');
    expect(/[А-Яа-яЁё]/u.test(value), key).toBe(false);
    expect([...value.matchAll(/\{p\d+\}/gu)].map(match => match[0]).sort(), key)
      .toEqual([...key.matchAll(/\{p\d+\}/gu)].map(match => match[0]).sort());
  }
  for (const name of fs.readdirSync('src').filter(name => /\.tsx?$/u.test(name))) {
    const source = ts.createSourceFile(name, fs.readFileSync(`src/${name}`, 'utf8'), ts.ScriptTarget.Latest, true);
    const visit = (node: ts.Node) => {
      if (ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node) || ts.isJsxText(node)) {
        const text = node.text.trim();
        if (/[А-Яа-яЁё]/u.test(text) && !['ё', 'е', 'Русский'].includes(text)) {
          expect(Object.hasOwn(catalog, text), `${name}: ${text}`).toBe(true);
        }
      }
      ts.forEachChild(node, visit);
    };
    visit(source);
  }
});

test('translation keeps template-like paths literal and accepts punctuation in error details', () => {
  setLanguage('en');
  const name = 'папка.{p1}/$&/<script>';
  expect(t('Удалить {p0}', { p0: name })).toBe(`Delete ${name}`);
  expect(t(`Недопустимое значение настройки ${name}.`)).toBe(`Invalid value for setting ${name}.`);
  const notice = t('Настройки сохранены. Изменение размера OCR обновляет версию кэша.');
  setLanguage('ru');
  expect(t(notice)).toBe('Настройки сохранены. Изменение размера OCR обновляет версию кэша.');
  expect(t('Неизвестная ошибка от сторонней библиотеки')).toBe('Неизвестная ошибка от сторонней библиотеки');
});

test('keyboard language slider persists and preserves search state and original message text', async ({ page }) => {
  const message = { chat_id: 'synthetic-i18n', message_id: 1, timestamp: 1750000000,
    author: 'Найти', text: '<img src=x onerror=window.languageInjected=true> Точная фраза {p0}',
    kind: 'message', action: null, reply_to: null, forwarded_from: null, matches_filters: true,
    media: [], edited_timestamp: null };
  await page.route('**/api/search?**', route => {
    const params = new URL(route.request().url()).searchParams;
    expect(params.get('q')).toBe('Точная фраза {p0}');
    expect(params.getAll('modality')).toEqual(['text', 'ocr']);
    expect(params.get('exact')).toBe('true');
    return route.fulfill({ json: { results: [{ chat_id: message.chat_id, chat_name: 'Настройки и диагностика',
      message_id: 1, timestamp: message.timestamp, messages: [message], matched_by: ['words'] }],
      effective_mode: 'mixed', has_more: false, limit: 20,
      warnings: ['Смысловой поиск ещё не готов. Показаны результаты по словам.'] } });
  });
  await page.goto('/');
  await page.getByLabel('Поисковый запрос').fill('Точная фраза {p0}');
  await page.getByRole('checkbox', { name: 'Изображения', exact: true }).uncheck();
  await page.getByLabel('Точная фраза', { exact: true }).check();
  const slider = page.getByRole('slider');
  await slider.focus();
  await slider.press('End');
  await expect(page.locator('html')).toHaveAttribute('lang', 'en');
  await expect(page).toHaveTitle('Archive · Telegram Search');
  await expect(slider).toHaveAccessibleName('Application language');
  await expect(slider).toHaveAttribute('aria-valuetext', 'English');
  await expect(page.getByLabel('Search query')).toHaveValue('Точная фраза {p0}');
  await expect(page.getByLabel('Exact phrase', { exact: true })).toBeChecked();
  await expect(page.getByRole('checkbox', { name: 'Images', exact: true })).not.toBeChecked();
  await page.getByRole('button', { name: 'Search', exact: true }).click();
  await expect(page.locator('.result-card')).toContainText(message.text);
  await expect(page.locator('.result-header')).toContainText('Настройки и диагностика');
  await expect(page.locator('.message-meta strong')).toHaveText('Найти');
  await expect(page.locator('.match-reasons')).toHaveText('Matching words');
  await expect(page.locator('.warning[role="status"]')).toContainText('Semantic search');
  expect(await page.evaluate(() => (window as unknown as Record<string, unknown>).languageInjected)).toBeUndefined();
  await slider.press('Home');
  await expect(page.locator('.result-card')).toContainText(message.text);
  await expect(page.locator('.match-reasons')).toHaveText('Совпали слова');
  await slider.press('End');
  await page.reload();
  await expect(page.locator('html')).toHaveAttribute('lang', 'en');
  await expect(page.getByRole('button', { name: 'Import archive' })).toBeVisible();
});

test('open settings switch languages without losing edits and translate backend errors', async ({ page }) => {
  await page.goto('/');
  await page.getByRole('button', { name: '⚙ Настройки и диагностика' }).click();
  await page.getByLabel('Количество результатов', { exact: true }).fill('7');
  await page.getByRole('slider').focus();
  await page.getByRole('slider').press('End');
  const dialog = page.getByRole('dialog');
  await expect(dialog).toHaveAccessibleName('Settings and diagnostics');
  await expect(page.getByLabel('Result limit', { exact: true })).toHaveValue('7');
  for (const title of ['Search results', 'Semantic search', 'Photos and OCR', 'Resources', 'Sources', 'Disk usage']) {
    await expect(dialog.getByRole('heading', { name: title, exact: true })).toBeVisible();
  }
  await expect(page.getByRole('button', { name: 'Prepare model and index', exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Prepare OCR', exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Prepare photo search', exact: true })).toBeVisible();
  await page.route('**/api/settings', async route => {
    if (route.request().method() !== 'PATCH') { await route.continue(); return; }
    await route.fulfill({ status: 400, json: { detail: 'Недопустимое значение настройки search_result_limit.' } });
  });
  await page.getByRole('button', { name: 'Save search settings' }).click();
  await expect(dialog.getByRole('alert')).toContainText('Invalid value for setting search_result_limit.');
  await page.getByRole('slider').focus();
  await page.getByRole('slider').press('Home');
  await expect(page.getByLabel('Количество результатов', { exact: true })).toHaveValue('7');
  await expect(dialog.getByRole('alert')).toContainText('Недопустимое значение настройки search_result_limit.');
});

test('English import, preview, apply and conflict resolution keep original export content', async ({ page }) => {
  await page.addInitScript(() => window.localStorage.setItem('bts.language', 'en'));
  await page.goto('/');
  const token = (await (await page.request.get('/api/session')).json()).token;
  const headers = { 'X-Session-Token': token };
  const prepared = await page.request.post('/api/imports', { headers, data: {
    json_path: path.resolve('../tests/fixtures/synthetic/chat.json'), scope: 'i18n',
  } });
  expect(prepared.ok()).toBe(true);
  const job = await prepared.json();
  await expect.poll(async () => {
    const jobs = await (await page.request.get('/api/imports')).json();
    return jobs.find((item: { id: string }) => item.id === job.id)?.state;
  }).toBe('completed');
  try {
    await page.getByRole('button', { name: 'Import archive' }).click();
    await page.getByLabel('JSON file path', { exact: true }).fill(path.resolve('../tests/fixtures/synthetic/conflict-chat.json'));
    await page.getByRole('textbox', { name: /^Account scope/ }).fill('i18n');
    await page.getByRole('button', { name: 'Check export', exact: true }).click();
    await expect(page.getByRole('heading', { name: 'Report ready' })).toBeVisible();
    await expect(page.locator('.preview-report')).toContainText('Conflicts');
    await page.getByRole('button', { name: 'Apply changes' }).click();
    await expect(page.getByRole('dialog')).toHaveCount(0);
    await page.getByRole('button', { name: 'Resolve conflicts' }).click();
    await expect(page.getByRole('dialog')).toHaveAccessibleName('Import conflicts');
    await expect(page.locator('.conflict-versions')).toContainText('В воскресенье поедем в лес.');
    await expect(page.locator('.conflict-versions')).toContainText('<img src=x onerror=window.conflictInjected=true>');
    expect(await page.evaluate(() => (window as unknown as Record<string, unknown>).conflictInjected)).toBeUndefined();
    await page.getByRole('button', { name: 'Keep current version' }).click();
    await expect(page.getByRole('dialog')).toContainText('All conflicts have been resolved.');
  } finally {
    const deleted = await page.request.delete(`/api/chats/${job.chat_id}`, { headers });
    expect(deleted.ok()).toBe(true);
  }
});

test('English errors and notices update after toggling while entered paths stay literal', async ({ page }) => {
  await page.goto('/');
  await page.getByRole('button', { name: 'Импортировать выгрузку' }).click();
  const source = '/синтетическая папка.{p0}/result.json';
  await page.getByLabel('Путь к JSON', { exact: true }).fill(source);
  await page.route('**/api/import-previews', async route => {
    if (route.request().method() !== 'POST') { await route.continue(); return; }
    expect(route.request().postDataJSON().json_path).toBe(source);
    await route.fulfill({ status: 400, json: { detail: 'JSON должен находиться внутри существующей папки экспорта.' } });
  });
  await page.getByRole('button', { name: 'Проверить экспорт', exact: true }).click();
  await expect(page.getByRole('alert')).toContainText('JSON должен находиться');
  await page.getByRole('slider').focus();
  await page.getByRole('slider').press('End');
  await expect(page.getByLabel('JSON file path', { exact: true })).toHaveValue(source);
  await expect(page.getByRole('alert')).toContainText('The JSON file must be inside an existing export folder.');
});

test('blocked language storage and a narrow viewport still allow an accessible slider', async ({ page }) => {
  await page.addInitScript(() => {
    Object.defineProperty(window, 'localStorage', { get: () => { throw new DOMException('Blocked', 'SecurityError'); } });
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/');
  await expect(page.locator('html')).toHaveAttribute('lang', 'ru');
  await page.getByRole('slider').focus();
  await page.getByRole('slider').press('End');
  await expect(page.locator('html')).toHaveAttribute('lang', 'en');
  await expect(page.getByRole('slider')).toBeVisible();
  const dimensions = await page.evaluate(() => ({ content: document.documentElement.scrollWidth, viewport: window.innerWidth }));
  expect(dimensions.content).toBeLessThanOrEqual(dimensions.viewport);
  await page.reload();
  await expect(page.locator('html')).toHaveAttribute('lang', 'ru');
});
