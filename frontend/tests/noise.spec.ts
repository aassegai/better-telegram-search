import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { test, expect } from './fixtures';
import { mockNoise, openNoise, readyNoiseJob } from './noise-mocks';

for (const english of [false, true]) {
  test(`noise report preserves edits across polling/pages and explicitly applies (${english ? 'EN' : 'RU'})`, async ({ page }) => {
    const state = await mockNoise(page);
    const panel = await openNoise(page, english);
    await panel.getByRole('button', { name: english ? 'Run analysis' : 'Запустить анализ', exact: true }).click();
    await expect(panel.getByRole('progressbar')).toBeVisible();
    await expect(panel.getByText(english ? 'Report ready' : 'Отчёт готов', { exact: true })).toBeVisible({ timeout: 10000 });
    expect(state.writes[0].body).toEqual({ max_messages: 1000000, time_limit_seconds: 300 });
    await panel.getByRole('button', { name: english ? 'Review report' : 'Просмотреть отчёт' }).click();
    const typo = panel.getByRole('combobox', { name: english ? 'Decision for бял' : 'Решение для бял', exact: true });
    await typo.selectOption('omit');
    await panel.getByRole('combobox', { name: english ? 'Analysis decisions' : 'Решения анализа', exact: true }).selectOption('');
    await expect(typo).toHaveValue('omit');
    await panel.getByRole('combobox', { name: english ? 'Decision for сука' : 'Решение для сука', exact: true }).selectOption('keep');
    const polled = state.statusCalls;
    await expect.poll(() => state.statusCalls).toBeGreaterThan(polled);
    await expect(typo).toHaveValue('omit');
    await panel.getByRole('button', { name: english ? 'Next entries' : 'Следующие записи' }).click();
    await expect(panel.getByRole('combobox', { name: english ? 'Decision for constructor' : 'Решение для constructor', exact: true })).toHaveValue('');
    await panel.getByRole('button', { name: english ? 'Previous entries' : 'Предыдущие записи' }).click();
    await expect(typo).toHaveValue('omit');
    await panel.getByRole('button', { name: english ? 'Save overrides' : 'Сохранить переопределения' }).click();
    expect(state.overrides).toEqual({ бял: 'omit', сука: 'keep' });
    await panel.getByRole('button', { name: english ? 'Preview rules' : 'Предпросмотр правил' }).click();
    await expect(panel.getByText(english ? 'Additional exact exclusions: 1 · keep rules: 1' : 'Дополнительных точных исключений: 1 · правил сохранения: 1')).toBeVisible();
    await panel.getByRole('button', { name: english ? 'Apply rules and rebuild text' : 'Применить правила и перестроить текст' }).click();
    await expect(panel.getByText(english ? 'Rules selected. The text index will be rebuilt, preserving its paused state.' : 'Правила выбраны. Текстовый индекс будет перестроен с сохранением паузы.')).toBeVisible();
    expect(state.writes.filter(item => item.path.endsWith('/apply'))[0].body).toEqual({ revision: 1 });
    expect(state.writes.some(item => item.path.includes('/index/settings'))).toBe(false);
  });
}

test('stale active analysis can be cancelled, and partial/old reports stay read-only', async ({ page }) => {
  const state = await mockNoise(page, true);
  state.job = { ...readyNoiseJob(), state: 'stale', stale: true, active: true };
  const panel = await openNoise(page);
  await panel.getByRole('button', { name: 'Отменить анализ' }).click();
  await expect(panel.getByRole('button', { name: 'Отменить анализ' })).toHaveCount(0);
  await expect(panel.getByRole('button', { name: 'Просмотреть отчёт' })).toBeDisabled();
  await expect(panel.getByRole('button', { name: 'Запустить анализ', exact: true })).toBeEnabled();
  state.job = { ...readyNoiseJob(), state: 'limited', summary: { ...readyNoiseJob().summary, complete: false, limits: ['message_limit'] } };
  await panel.getByRole('button', { name: 'Обновить анализы' }).click();
  await panel.getByRole('button', { name: 'Просмотреть отчёт' }).click();
  await expect(panel.getByRole('combobox', { name: 'Решение для бял', exact: true })).toBeDisabled();
  await expect(panel.getByRole('button', { name: 'Предпросмотр правил' })).toBeDisabled();
  state.job = readyNoiseJob(); delete (state.job.summary as { report_sha256?: string }).report_sha256;
  await panel.getByRole('button', { name: 'Обновить анализы' }).click();
  await expect(panel.getByText('Для применения этого отчёта нужен новый анализ.')).toBeVisible();
});

test('closing and reopening settings ignores a held report response', async ({ page }) => {
  const state = await mockNoise(page, true);
  let release!: () => void;
  state.formsGate = new Promise<void>(resolve => { release = resolve; });
  const panel = await openNoise(page);
  await panel.getByRole('button', { name: 'Просмотреть отчёт' }).click();
  await expect.poll(() => state.formsStarted).toBe(1);
  await page.keyboard.press('Escape');
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await page.getByRole('button', { name: /^Настройки индексации / }).first().click();
  await page.locator('.noise-panel > details > summary').click();
  release();
  await expect(page.locator('.noise-form')).toHaveCount(0);
  await expect(page.locator('.noise-panel').getByRole('alert')).toHaveCount(0);
});

test('report strings are literal and a prototype-looking error code cannot crash the panel', async ({ page }) => {
  const state = await mockNoise(page, true);
  const injected = '<img src=x onerror=window.noiseInjected=true>';
  state.forms[0].form = injected; state.forms[0].candidate.unchanged.form = injected;
  const panel = await openNoise(page, true);
  await panel.getByRole('button', { name: 'Review report' }).click();
  await expect(panel.locator('.noise-form bdi').first()).toHaveText(injected);
  expect(await panel.locator('img').count()).toBe(0);
  expect(await page.evaluate(() => (window as unknown as Record<string, unknown>).noiseInjected)).toBeUndefined();
  state.job = { ...readyNoiseJob(), state: 'failed', error: 'constructor' };
  await panel.getByRole('button', { name: 'Refresh analyses' }).click();
  await expect(panel.getByRole('alert')).toHaveText('Analysis failed. Run it again.');
});

test('real synthetic API report is applied from the chat UI without model preparation', async ({ page }) => {
  const temporary = await fs.mkdtemp(path.join(os.tmpdir(), 'bts-noise-ui-'));
  const filename = path.join(temporary, 'result.json');
  await fs.writeFile(filename, JSON.stringify({ id: 9009501, name: 'Synthetic noise UI', type: 'personal_chat', messages: [
    { id: 1, type: 'message', date_unixtime: '1750000000', from: 'Synthetic author', from_id: 'user1', text: 'Содержательная фраза' },
    { id: 2, type: 'message', date_unixtime: '1750000001', from: 'Synthetic author', from_id: 'user1', text: 'бял' },
    { id: 3, type: 'message', date_unixtime: '1750000002', from: 'Synthetic author', from_id: 'user1', text: 'сука' },
  ] }));
  const token = (await (await page.request.get('/api/session')).json()).token;
  const headers = { 'X-Session-Token': token };
  let chatId = '';
  try {
    const imported = await page.request.post('/api/imports', { headers, data: { json_path: filename } });
    expect(imported.status()).toBe(202);
    const id = (await imported.json()).id;
    await expect.poll(async () => {
      const result = await (await page.request.get(`/api/imports/${id}`)).json();
      if (result.state === 'completed') chatId = result.chat_id;
      return result.state;
    }).toBe('completed');
    expect((await page.request.post(`/api/chats/${chatId}/index/text/pause`, { headers })).ok()).toBeTruthy();
    const modelWrites: string[] = [];
    page.on('request', request => { if (request.method() === 'POST' && /\/(models|media)\/.*prepare/u.test(request.url())) modelWrites.push(request.url()); });
    await page.goto('/');
    await page.getByRole('button', { name: 'Настройки индексации Synthetic noise UI', exact: true }).click();
    const panel = page.locator('.noise-panel');
    await panel.locator(':scope > details > summary').click();
    await panel.getByRole('button', { name: 'Запустить анализ', exact: true }).click();
    await expect(panel.getByText('Отчёт готов', { exact: true })).toBeVisible({ timeout: 15000 });
    await panel.getByRole('button', { name: 'Просмотреть отчёт' }).click();
    await panel.getByRole('combobox', { name: 'Решение для бял', exact: true }).selectOption('omit');
    await panel.getByRole('combobox', { name: 'Решения анализа', exact: true }).selectOption('');
    await panel.getByRole('combobox', { name: 'Решение для сука', exact: true }).selectOption('keep');
    await panel.getByRole('button', { name: 'Сохранить переопределения' }).click();
    await panel.getByRole('button', { name: 'Предпросмотр правил' }).click();
    await panel.getByRole('button', { name: 'Применить правила и перестроить текст' }).click();
    await expect(panel.getByText('Эти правила выбраны для диалога.')).toBeVisible({ timeout: 10000 });
    const after = await (await page.request.get(`/api/chats/${chatId}/index`)).json();
    expect(after.semantic.paused).toBeTruthy();
    const found = await (await page.request.get('/api/search', { params: { q: 'бял', chat_id: chatId, mode: 'words' } })).json();
    expect(found.results[0].message_id).toBe(2);
    await panel.getByRole('button', { name: 'Запустить анализ', exact: true }).click();
    await expect(panel.getByText('Отчёт готов', { exact: true })).toBeVisible({ timeout: 15000 });
    await panel.getByRole('button', { name: 'Просмотреть отчёт' }).click();
    await expect(panel.getByText('Ранее выбранное правило: Исключать с учётом защит')).toBeVisible();
    await expect(panel.getByText('Правило проекта: Исключать с учётом защит')).toBeVisible();
    await panel.getByRole('button', { name: 'Предпросмотр правил' }).click();
    await panel.getByRole('button', { name: 'Применить правила и перестроить текст' }).click();
    await expect(panel.getByText('Эти правила уже используются. Повторное перестроение не запущено.')).toBeVisible();
    expect(modelWrites).toEqual([]);
  } finally {
    if (chatId) await page.request.delete(`/api/chats/${chatId}`, { headers });
    await fs.rm(temporary, { recursive: true, force: true });
  }
});

test('refresh invalidates a preview when another window changes the draft revision', async ({ page }) => {
  const state = await mockNoise(page, true);
  const panel = await openNoise(page);
  await panel.getByRole('button', { name: 'Предпросмотр правил' }).click();
  await expect(panel.getByText('Дополнительных точных исключений: 0 · правил сохранения: 0')).toBeVisible();
  state.revision = 1; state.overrides = { бял: 'omit' };
  await panel.getByRole('button', { name: 'Обновить анализы' }).click();
  await expect(panel.getByRole('button', { name: 'Применить правила и перестроить текст' })).toHaveCount(0);
  expect(state.writes.filter(write => write.path.endsWith('/apply'))).toEqual([]);
  await panel.getByRole('button', { name: 'Предпросмотр правил' }).click();
  await expect(panel.getByText('Дополнительных точных исключений: 1 · правил сохранения: 0')).toBeVisible();
  await panel.getByRole('button', { name: 'Применить правила и перестроить текст' }).click();
  expect(state.writes.filter(write => write.path.endsWith('/preview')).map(write => write.body?.revision)).toEqual([0, 1]);
  expect(state.writes.find(write => write.path.endsWith('/apply'))?.body).toEqual({ revision: 1 });
});

test('inherited rules are distinct from analysis suggestions and local overrides', async ({ page }) => {
  const state = await mockNoise(page, true);
  state.forms[0].inherited_decision = state.forms[0].default_decision = 'omit';
  state.forms[0].default_origin = 'inherited';
  state.forms[1].inherited_decision = state.forms[1].default_decision = 'keep';
  state.forms[1].default_origin = 'inherited';
  const panel = await openNoise(page);
  await panel.getByRole('button', { name: 'Просмотреть отчёт' }).click();
  const typo = panel.locator('.noise-form').filter({ hasText: 'Решение для бял' });
  await expect(typo.getByText('Решение анализа: Сохранить · неоднозначно')).toBeVisible();
  await expect(typo.getByText('Правило проекта: Исключать с учётом защит')).toBeVisible();
  await expect(typo.getByText('Ранее выбранное правило: Исключать с учётом защит')).toBeVisible();
  await expect(typo.getByRole('combobox')).toHaveValue('');
  await typo.getByRole('combobox').selectOption('keep');
  await expect(typo.getByText('Правило проекта: Сохранять')).toBeVisible();
  await panel.getByRole('combobox', { name: 'Решения анализа', exact: true }).selectOption('');
  const seed = panel.locator('.noise-form').filter({ hasText: 'Решение для сука' });
  await expect(seed.getByText('Решение анализа: Исключить из смыслового текста')).toBeVisible();
  await expect(seed.getByText('Правило проекта: Сохранять')).toBeVisible();
  await expect(seed.getByText('Ранее выбранное правило: Сохранять')).toBeVisible();
  expect(state.overrides).toEqual({});
});

test('family and hypothesis details keep words literal and explain the operation', async ({ page }) => {
  await mockNoise(page, true);
  const panel = await openNoise(page, true);
  await panel.getByRole('button', { name: 'Review report' }).click();
  await panel.locator('.noise-evidence > summary').first().click();
  await expect(panel.getByText('Adjacent character swap', { exact: false })).toBeVisible();
  await expect(panel.locator('.noise-evidence')).toContainText('бял');
  await expect(panel.locator('.noise-evidence')).toContainText('был');
  await panel.getByRole('combobox', { name: 'Report section', exact: true }).selectOption('families');
  await expect(panel.getByText('Family statistics do not mean all variants are excluded.')).toBeVisible();
  await expect(panel.locator('.noise-form > strong > bdi')).toHaveText('бля');
});

test('a draft conflict preserves edits until explicit discard and fresh revision', async ({ page }) => {
  const state = await mockNoise(page, true);
  const panel = await openNoise(page);
  await panel.getByRole('button', { name: 'Просмотреть отчёт' }).click();
  const choice = panel.getByRole('combobox', { name: 'Решение для бял', exact: true });
  await choice.selectOption('omit');
  state.revision = 1; state.overrides = { бял: 'keep' };
  await panel.getByRole('button', { name: 'Сохранить переопределения' }).click();
  await expect(panel.getByRole('alert')).toHaveText('Проект изменился. Обновите его перед сохранением.');
  await expect(choice).toHaveValue('omit');
  await expect(panel.getByRole('button', { name: 'Предпросмотр правил' })).toBeDisabled();
  await panel.getByRole('button', { name: 'Отменить правки' }).click();
  await expect(choice).toHaveValue('keep');
  await choice.selectOption('omit');
  await panel.getByRole('button', { name: 'Сохранить переопределения' }).click();
  expect(state.revision).toBe(2);
  expect(state.writes.filter(write => write.method === 'PATCH').map(write => write.body?.revision)).toEqual([0, 1]);
});

test('collapsing the panel suppresses a held read and reopening reloads safely', async ({ page }) => {
  const state = await mockNoise(page, true);
  let release!: () => void; state.formsGate = new Promise<void>(resolve => { release = resolve; });
  const panel = await openNoise(page);
  await panel.getByRole('button', { name: 'Просмотреть отчёт' }).click();
  await expect.poll(() => state.formsStarted).toBe(1);
  const summary = panel.locator(':scope > details > summary');
  await summary.click();
  await summary.click();
  release();
  await expect(panel.getByRole('button', { name: 'Просмотреть отчёт' })).toBeEnabled();
  await expect(panel.locator('.noise-form')).toHaveCount(0);
  await expect(panel.getByRole('alert')).toHaveCount(0);
});

test('changing sender exclusions discards evidence from an earlier scope', async ({ page }) => {
  const state = await mockNoise(page, true);
  let changed = false;
  await page.route('**/api/chats/*/index', async route => {
    const response = await route.fetch();
    const value = await response.json();
    if (changed) value.excluded_author_ids = ['synthetic-excluded'];
    await route.fulfill({ json: value });
  });
  let release!: () => void; state.formsGate = new Promise<void>(resolve => { release = resolve; });
  const panel = await openNoise(page);
  await panel.getByRole('button', { name: 'Просмотреть отчёт' }).click();
  await expect.poll(() => state.formsStarted).toBe(1);
  changed = true; state.job = { ...readyNoiseJob(), stale: true, state: 'stale' };
  await expect(panel.getByText('Диалог или исключения изменились. Запустите анализ заново.')).toBeVisible({ timeout: 10000 });
  release();
  await expect(panel.locator('.noise-form')).toHaveCount(0);
  await expect(panel.getByRole('button', { name: 'Просмотреть отчёт' })).toBeDisabled();
});

for (const english of [false, true]) for (const dark of [false, true]) {
  test(`a 64-character form fits a narrow report (${english ? 'EN' : 'RU'}, ${dark ? 'dark' : 'light'})`, async ({ page }) => {
    await page.setViewportSize({ width: 320, height: 800 });
    await page.emulateMedia({ colorScheme: dark ? 'dark' : 'light' });
    const state = await mockNoise(page, true);
    const long = 'a'.repeat(64);
    state.forms[0].form = long; state.forms[0].candidate.unchanged.form = long;
    const panel = await openNoise(page, english);
    await panel.getByRole('button', { name: english ? 'Review report' : 'Просмотреть отчёт' }).click();
    await expect(panel.getByRole('combobox', { name: english ? `Decision for ${long}` : `Решение для ${long}`, exact: true })).toBeVisible();
    const sizes = await panel.evaluate(element => {
      const modal = element.closest('.modal')!;
      return { panelWidth: element.clientWidth, panelScroll: element.scrollWidth, modalWidth: modal.clientWidth, modalScroll: modal.scrollWidth };
    });
    expect(sizes.panelScroll).toBeLessThanOrEqual(sizes.panelWidth + 1);
    expect(sizes.modalScroll).toBeLessThanOrEqual(sizes.modalWidth + 1);
  });
}
