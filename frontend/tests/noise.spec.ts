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

for (const english of [false, true]) {
  test(`complete source with limited hypotheses can apply exact rules (${english ? 'EN' : 'RU'})`, async ({ page }) => {
    const state = await mockNoise(page, true);
    state.job = readyNoiseJob({ coverage_version: 'source-and-evidence-v1', source_complete: true, evidence_complete: false, policy_applicable: true, new_auto_omit_enabled: false, limited_evidence_forms: 2 });
    state.job.summary.complete = false; state.job.summary.limits = ['candidate_limit'];
    const panel = await openNoise(page, english);
    await expect(panel.getByText(english ? 'All messages in scope were processed. The hypothesis list is limited; ambiguous forms are kept. You can apply exact rules.' : 'Все сообщения области обработаны. Список гипотез ограничен; спорные формы сохраняются. Можно применить точные правила.')).toBeVisible();
    await panel.getByRole('button', { name: english ? 'Review report' : 'Просмотреть отчёт' }).click();
    const rule = panel.getByRole('combobox', { name: english ? 'Decision for бял' : 'Решение для бял', exact: true });
    await expect(rule).toBeEnabled(); await expect(rule).toHaveValue('');
    await panel.getByRole('button', { name: english ? 'Preview rules' : 'Предпросмотр правил' }).click();
    await panel.getByRole('button', { name: english ? 'Apply rules and rebuild text' : 'Применить правила и перестроить текст' }).click();
    expect(state.writes.find(write => write.path.endsWith('/apply'))?.body).toEqual({ revision: 0 });
    expect(state.overrides).toEqual({});
  });
}

for (const invalid of ['old-partial', 'source-partial', 'unknown-version', 'new-auto-gate', 'legacy-auto-true', 'legacy-auto-missing', 'hybrid-without-version']) {
  test(`inconsistent or old limited coverage stays read-only: ${invalid}`, async ({ page }) => {
    const state = await mockNoise(page, true);
    state.job = invalid === 'old-partial' ? readyNoiseJob() : readyNoiseJob({ coverage_version: 'source-and-evidence-v1', source_complete: true, evidence_complete: false, policy_applicable: true, new_auto_omit_enabled: false });
    state.job.summary.complete = false; state.job.summary.limits = ['candidate_limit'];
    if (invalid === 'source-partial') state.job.summary.limits.push('message_limit');
    if (invalid === 'unknown-version') state.job.summary.coverage_version = 'unknown';
    if (invalid === 'new-auto-gate') state.job.summary.new_auto_omit_enabled = true;
    if (invalid.startsWith('legacy-auto-')) {
      state.job = readyNoiseJob();
      if (invalid === 'legacy-auto-true') state.job.summary.new_auto_omit_enabled = true;
      else delete state.job.summary.new_auto_omit_enabled;
    }
    if (invalid === 'hybrid-without-version') {
      state.job.summary.complete = true; state.job.summary.limits = [];
      delete state.job.summary.coverage_version;
    }
    const panel = await openNoise(page);
    await panel.getByRole('button', { name: 'Просмотреть отчёт' }).click();
    await expect(panel.getByText('Для применения этого отчёта нужен новый анализ.')).toBeVisible();
    await expect(panel.getByRole('combobox', { name: 'Решение для бял', exact: true })).toBeDisabled();
    await expect(panel.getByRole('button', { name: 'Предпросмотр правил' })).toBeDisabled();
    expect(state.writes).toEqual([]);
  });
}

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
    { id: 4, type: 'message', date_unixtime: '1750000003', from: 'Synthetic author', from_id: 'user1', text: 'да' },
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
    await expect(panel.getByText('Отчёт готов с ограничениями', { exact: true })).toBeVisible({ timeout: 15000 });
    await expect(panel.getByText('Все сообщения области обработаны. Список гипотез ограничен; спорные формы сохраняются. Можно применить точные правила.')).toBeVisible();
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
    await expect(panel.getByText('Отчёт готов с ограничениями', { exact: true })).toBeVisible({ timeout: 15000 });
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


for (const english of [false, true]) for (const legacy of [false, true]) {
  test(`partial noise reports allow exact rules without pretending full coverage (${english ? 'EN' : 'RU'}/${legacy ? 'v072' : 'new'})`, async ({ page }) => {
    const state = await mockNoise(page, true);
    state.job = readyNoiseJob({ coverage_version: legacy ? 'source-and-evidence-v1' : 'bounded-exact-rules-v2',
      source_complete: false, evidence_complete: false, policy_applicable: !legacy });
    state.job.state = 'limited'; state.job.summary.complete = false;
    state.job.summary.limits = legacy ? ['candidate_limit', 'text_limit'] : ['candidate_limit', 'time_limit'];
    Object.assign(state.job.summary, { limited_messages: 8, skipped_long_messages: 14 });
    if (!legacy) state.job.error = 'time_limit';
    const panel = await openNoise(page, english);
    await expect(panel.getByText(english ? 'Partial report' : 'Частичный отчёт', { exact: true })).toBeVisible();
    await expect(panel.getByText(english ? 'Messages skipped with five or more unique words: 14' : 'Пропущено сообщений с пятью и более уникальными словами: 14', { exact: true })).toBeVisible();
    await expect(panel.getByText(english ? 'The analysis is partial. You can review the report, choose exact rules and apply them. New unexamined forms are not excluded automatically.' : 'Анализ ограничен. Можно просмотреть отчёт, выбрать точные правила и применить их. Новые неизученные формы автоматически не исключаются.', { exact: true })).toBeVisible();
    await panel.getByRole('button', { name: english ? 'Review report' : 'Просмотреть отчёт' }).click();
    const choice = panel.getByRole('combobox', { name: english ? 'Decision for бял' : 'Решение для бял', exact: true });
    await expect(choice).toBeEnabled();
    await choice.selectOption('keep');
    await panel.getByRole('button', { name: english ? 'Save overrides' : 'Сохранить переопределения' }).click();
    await panel.getByRole('button', { name: english ? 'Preview rules' : 'Предпросмотр правил' }).click();
    await panel.getByRole('button', { name: english ? 'Apply rules and rebuild text' : 'Применить правила и перестроить текст' }).click();
    expect(state.overrides).toEqual({ бял: 'keep' });
    expect(state.writes.some(write => write.path.endsWith('/apply'))).toBe(true);
  });
}

for (const english of [false, true]) {
  test(`whole-message families and finished scan limits are explicit (${english ? 'EN' : 'RU'})`, async ({ page }) => {
    const state = await mockNoise(page, true);
    state.job = readyNoiseJob({ complete: false, limits: ['text_limit', 'candidate_limit'],
      coverage_version: 'bounded-exact-rules-v2', source_complete: false, evidence_complete: false,
      policy_applicable: true, new_auto_omit_enabled: false, message_forms_enabled: true,
      message_auto_omit_enabled: true, scan_complete: true, scanned_messages: 312142,
      whole_messages: 150000, forms: 5144, limited_messages: 8 });
    state.job.state = 'limited';
    Object.assign(state.forms[0], { form: 'покормить жабу', form_kind: 'message',
      default_decision: 'omit', candidate: { ...state.forms[0].candidate, decision: 'omit',
        reason: 'approved_message_seed', unchanged: { ...state.forms[0].candidate.unchanged, form: 'покормить жабу' } } });
    const panel = await openNoise(page, english);
    await expect(panel.getByText(english ? 'Report ready with limits' : 'Отчёт готов с ограничениями', { exact: true })).toBeVisible();
    await expect(panel.getByRole('status').filter({ hasText: english ? 'The dialog scan is complete.' : 'Проход по диалогу завершён.' })).toBeVisible();
    await expect(panel.getByText(english ? 'Messages scanned: 312142 · short messages: 150000 · variants: 5144' : 'Просмотрено сообщений: 312142 · коротких: 150000 · вариантов: 5144', { exact: true })).toBeVisible();
    await panel.getByRole('button', { name: english ? 'Review report' : 'Просмотреть отчёт' }).click();
    await panel.getByRole('combobox', { name: english ? 'Analysis decisions' : 'Решения анализа', exact: true }).selectOption('');
    await expect(panel.getByText(english ? 'Whole message · the rule does not remove individual words from phrases' : 'Целое сообщение · правило не удаляет отдельные слова из фраз', { exact: true })).toBeVisible();
    const command = panel.getByRole('article').filter({ hasText: 'покормить жабу' }).first();
    await command.getByText(english ? 'Why this decision' : 'Почему такое решение', { exact: true }).click();
    await expect(command.getByText(english ? 'Confirmed whole command' : 'Подтверждённая команда целиком', { exact: true })).toBeVisible();
    await expect(command.getByText(english ? 'Similar messages and families' : 'Похожие сообщения и семейства', { exact: true })).toBeVisible();
    await panel.getByRole('combobox', { name: english ? 'Decision for покормить жабу' : 'Решение для покормить жабу', exact: true }).selectOption('keep');
    await panel.getByRole('button', { name: english ? 'Save overrides' : 'Сохранить переопределения' }).click();
    expect(state.overrides['покормить жабу']).toBe('keep');
    expect(state.writes.some(item => item.path.endsWith('/apply'))).toBe(false);
  });
}

test('active whole-message analysis labels the short-message counter before a summary exists', async ({ page }) => {
  const state = await mockNoise(page, true);
  state.job = { ...readyNoiseJob(), state: 'running', active: true, summary: null!,
    progress: { ...readyNoiseJob().progress, message_forms_enabled: true, scanned_messages: 100, whole_messages: 75, forms: 20 } };
  state.autoFinish = false;
  const panel = await openNoise(page);
  await expect(panel.getByText('Просмотрено сообщений: 100 · коротких: 75 · вариантов: 20', { exact: true })).toBeVisible();
  await expect(panel.getByRole('button', { name: 'Отменить анализ' })).toBeEnabled();
});

for (const english of [false, true]) {
  test(`whole family opens all variants and applies independent keep/omit decisions (${english ? 'EN' : 'RU'})`, async ({ page }) => {
    const state = await mockNoise(page, true);
    state.forms = state.forms.slice(0, 2);
    const seed = 'покормить жабу';
    state.families = [{ seed, observed_count: 8, eligible_count: 8, omit_count: 4, review_count: 4, keep_count: 0 }];
    for (const [index, text] of [seed, 'покорить жабу'].entries()) {
      Object.assign(state.forms[index], { form: text, form_kind: 'message', default_decision: index ? 'keep' : 'omit',
        candidate: { ...state.forms[index].candidate, decision: index ? 'review' : 'omit',
          noise: [{ form: seed, operation: index ? 'deletion' : 'exact', corpus_count: 4, score: 1 }],
          unchanged: { ...state.forms[index].candidate.unchanged, form: text } } });
    }
    const panel = await openNoise(page, english);
    await panel.getByRole('button', { name: english ? 'Message families and variants' : 'Семейства и варианты сообщений' }).click();
    const open = panel.getByRole('button', { name: english ? 'Open entire family' : 'Открыть всё семейство' });
    await expect(open).toHaveAttribute('aria-expanded', 'false');
    await open.click();
    const original = panel.getByRole('combobox', { name: english ? `Decision for ${seed}` : `Решение для ${seed}`, exact: true });
    const variant = panel.getByRole('combobox', { name: english ? 'Decision for покорить жабу' : 'Решение для покорить жабу', exact: true });
    await expect(original).toBeEnabled();
    await expect(variant).toBeEnabled();
    await original.selectOption('keep');
    await variant.selectOption('omit');
    await panel.getByRole('button', { name: english ? 'Hide variants' : 'Скрыть варианты' }).click();
    await panel.getByRole('button', { name: english ? 'Open entire family' : 'Открыть всё семейство' }).click();
    await expect(original).toHaveValue('keep');
    await expect(variant).toHaveValue('omit');
    expect(state.writes).toEqual([]);
    await panel.getByRole('button', { name: english ? 'Save overrides' : 'Сохранить переопределения' }).click();
    expect(state.overrides).toEqual({ [seed]: 'keep', 'покорить жабу': 'omit' });
    await panel.getByRole('button', { name: english ? 'Preview rules' : 'Предпросмотр правил' }).click();
    await panel.getByRole('button', { name: english ? 'Apply rules and rebuild text' : 'Применить правила и перестроить текст' }).click();
    expect(state.writes.find(item => item.path.endsWith('/apply'))?.body).toEqual({ revision: 1 });
  });
}

function bulkFamily(state: Awaited<ReturnType<typeof mockNoise>>, count: number) {
  const seed = 'покормить жабу';
  const example = state.forms[0];
  state.forms = Array.from({ length: count }, (_, index) => ({ ...example,
    form: `вариант команды ${index}`, form_kind: 'message' as const,
    candidate: { ...example.candidate, noise: [{ form: seed, operation: 'substitution', corpus_count: count, score: 1 }] } }));
  state.families = [{ seed, observed_count: count, eligible_count: count, omit_count: 0, review_count: count, keep_count: 0 }];
  return seed;
}

for (const english of [false, true]) {
  test(`omit entire family includes unopened pages and permits individual keep (${english ? 'EN' : 'RU'})`, async ({ page }) => {
    const state = await mockNoise(page, true);
    bulkFamily(state, 120);
    state.overrides = { 'правило другого семейства': 'keep', 'вариант команды 0': 'keep' };
    const panel = await openNoise(page, english);
    await panel.getByRole('button', { name: english ? 'Message families and variants' : 'Семейства и варианты сообщений' }).click();
    const before = state.formsStarted;
    await panel.getByRole('button', { name: english ? 'Omit entire family' : 'Исключить всё семейство' }).click();
    await expect(panel.getByRole('status').filter({ hasText: english ? 'Omission selected for 120' : 'Выбрано исключение для 120' })).toBeVisible();
    expect(state.formsStarted - before).toBe(2);
    expect(state.writes).toEqual([]);
    await panel.getByRole('button', { name: english ? 'Open entire family' : 'Открыть всё семейство' }).click();
    const first = panel.getByRole('combobox', { name: english ? 'Decision for вариант команды 0' : 'Решение для вариант команды 0', exact: true });
    await expect(first).toHaveValue('omit');
    await first.selectOption('keep');
    await panel.getByRole('button', { name: english ? 'Save overrides' : 'Сохранить переопределения' }).click();
    expect(Object.keys(state.overrides)).toHaveLength(121);
    expect(state.overrides['вариант команды 119']).toBe('omit');
    expect(state.overrides['вариант команды 0']).toBe('keep');
    expect(state.overrides['правило другого семейства']).toBe('keep');
    expect(state.writes.some(item => item.path.endsWith('/apply'))).toBe(false);
  });
}

test('oversized family does not partially change the draft', async ({ page }) => {
  const state = await mockNoise(page, true);
  bulkFamily(state, 1001);
  state.overrides = { 'вариант команды 0': 'keep' };
  const panel = await openNoise(page);
  await panel.getByRole('button', { name: 'Семейства и варианты сообщений' }).click();
  await panel.getByRole('button', { name: 'Исключить всё семейство' }).click();
  await expect(panel.getByRole('alert').filter({ hasText: 'Допускается не более 1000' })).toBeVisible();
  await expect(panel.getByRole('button', { name: 'Сохранить переопределения' })).toBeDisabled();
  expect(state.overrides).toEqual({ 'вариант команды 0': 'keep' });
  expect(state.writes).toEqual([]);
});

test('family bulk selection is discarded if the draft changed in another window', async ({ page }) => {
  const state = await mockNoise(page, true);
  bulkFamily(state, 120);
  state.overrides = { 'вариант команды 0': 'keep' };
  const panel = await openNoise(page);
  await panel.getByRole('button', { name: 'Семейства и варианты сообщений' }).click();
  let release!: () => void;
  state.formsGate = new Promise<void>(resolve => { release = resolve; });
  const before = state.formsStarted;
  await panel.getByRole('button', { name: 'Исключить всё семейство' }).click();
  await expect.poll(() => state.formsStarted).toBeGreaterThan(before);
  state.revision++;
  release(); state.formsGate = null;
  await expect(panel.getByRole('alert').filter({ hasText: 'Проект изменился' })).toBeVisible();
  expect(state.overrides).toEqual({ 'вариант команды 0': 'keep' });
  expect(state.writes).toEqual([]);
  await expect(panel.getByRole('button', { name: 'Сохранить переопределения' })).toBeDisabled();
});

test('collapsing cancels an unfinished whole-family selection', async ({ page }) => {
  const state = await mockNoise(page, true);
  bulkFamily(state, 120);
  const panel = await openNoise(page);
  await panel.getByRole('button', { name: 'Семейства и варианты сообщений' }).click();
  let release!: () => void;
  state.formsGate = new Promise<void>(resolve => { release = resolve; });
  const before = state.formsStarted;
  await panel.getByRole('button', { name: 'Исключить всё семейство' }).click();
  await expect.poll(() => state.formsStarted).toBeGreaterThan(before);
  await panel.locator(':scope > details > summary').click();
  release(); state.formsGate = null;
  await panel.locator(':scope > details > summary').click();
  await expect(panel.getByRole('button', { name: 'Сохранить переопределения' })).toBeDisabled();
  expect(state.overrides).toEqual({});
  expect(state.writes).toEqual([]);
});

test('family selection respects the serialized draft byte limit atomically', async ({ page }) => {
  const state = await mockNoise(page, true);
  bulkFamily(state, 1000);
  for (const [index, row] of state.forms.entries()) row.form = `${'𐐨'.repeat(130)} cmd${index}`;
  const panel = await openNoise(page);
  await panel.getByRole('button', { name: 'Семейства и варианты сообщений' }).click();
  await panel.getByRole('button', { name: 'Исключить всё семейство' }).click();
  await expect(panel.getByRole('alert').filter({ hasText: 'Проект фильтра слишком большой' })).toBeVisible();
  await expect(panel.getByRole('button', { name: 'Сохранить переопределения' })).toBeDisabled();
  expect(state.writes).toEqual([]);
});
