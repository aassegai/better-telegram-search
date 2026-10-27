import { expect, test } from '@playwright/test';
import { mockNoise, openNoise, readyComparison } from './noise-mocks';

for (const english of [false, true]) {
  test(`background comparison shows measured counts without applying rules (${english ? 'EN' : 'RU'})`, async ({ page }) => {
    const state = await mockNoise(page, true);
    const panel = await openNoise(page, english);
    const comparison = panel.getByRole('region', { name: english ? 'Chunk comparison' : 'Сравнение чанкинга', exact: true });
    await comparison.getByRole('button', { name: english ? 'Compare chunks and tokens' : 'Сравнить фрагменты и токены' }).click();
    const write = state.writes.find(item => item.path.endsWith('/dry-runs'));
    expect(write?.body).toEqual({ revision: 0, max_messages: 100000, time_limit_seconds: 300 });
    await expect(comparison.getByRole('button', { name: english ? 'Cancel comparison' : 'Отменить сравнение' })).toBeEnabled();
    await expect(panel.getByRole('button', { name: english ? 'Delete report' : 'Удалить отчёт' })).toBeDisabled();
    state.comparison = readyComparison();
    await expect(comparison.getByText(english ? 'Comparison ready' : 'Сравнение готово', { exact: true })).toBeVisible();
    await expect(comparison.getByText(english ? 'Change in chunks: -1 · tokens: -50' : 'Изменение фрагментов: -1 · токенов: -50')).toBeVisible();
    const tokens = comparison.getByRole('row').filter({ hasText: english ? /^Tokens/ : /^Токены/ }).first();
    await expect(tokens).toContainText('500'); await expect(tokens).toContainText('450');
    await comparison.getByText(english ? 'Measurement details' : 'Подробности измерения', { exact: true }).click();
    await expect(comparison.getByText(english ? 'Padding uses the configured batch size within UTC days. This does not estimate GPU speedup.' : 'Паддинг посчитан для настроенного батча по UTC-дням. Это не оценка ускорения GPU.')).toBeVisible();
    await expect(panel.getByRole('button', { name: english ? 'Delete report' : 'Удалить отчёт' })).toBeEnabled();
    expect(state.writes.some(item => item.path.endsWith('/apply') || item.path.includes('prepare'))).toBe(false);
  });
}

test('a stale active comparison remains cancellable', async ({ page }) => {
  const state = await mockNoise(page, true);
  state.comparison = { ...readyComparison(), state: 'stale', stale: true, active: true, summary: null!, error: 'source_changed' };
  const panel = await openNoise(page);
  const comparison = panel.getByRole('region', { name: 'Сравнение чанкинга', exact: true });
  await expect(comparison.getByRole('table')).toHaveCount(0);
  await comparison.getByRole('button', { name: 'Отменить сравнение' }).click();
  await expect(comparison.getByRole('button', { name: 'Отменить сравнение' })).toHaveCount(0);
  expect(state.writes.some(item => /\/dry-runs\/[^/]+\/cancel$/u.test(item.path))).toBe(true);
});

test('transient status failure preserves cancellation and parent protection in English', async ({ page }) => {
  const state = await mockNoise(page, true);
  state.comparison = { ...readyComparison(), state: 'running', active: true, summary: null! };
  const panel = await openNoise(page, true);
  const comparison = panel.getByRole('region', { name: 'Chunk comparison', exact: true });
  await expect(comparison.getByRole('button', { name: 'Cancel comparison' })).toBeVisible();
  await page.route('**/noise-analyses/*/dry-runs', async route => {
    if (route.request().method() === 'GET') return route.fulfill({ status: 503, json: { detail: 'База занята. Повторите запрос через несколько секунд.' } });
    return route.fallback();
  });
  await expect(comparison.getByRole('alert')).toHaveText('The database is busy. Try again in a few seconds.');
  await expect(panel.getByRole('button', { name: 'Delete report' })).toBeDisabled();
  await expect(comparison.getByRole('button', { name: 'Compare chunks and tokens' })).toBeDisabled();
  await comparison.getByRole('button', { name: 'Cancel comparison' }).click();
  expect(state.writes.some(item => /\/dry-runs\/[^/]+\/cancel$/u.test(item.path))).toBe(true);
});

test('initial status failure protects deletion and mutation errors are translated', async ({ page }) => {
  await mockNoise(page, true);
  let unavailable = true;
  await page.route('**/noise-analyses/*/dry-runs', async route => {
    if (route.request().method() === 'GET' && unavailable) return route.fulfill({ status: 503, json: { detail: 'База занята. Повторите запрос через несколько секунд.' } });
    if (route.request().method() === 'POST') return route.fulfill({ status: 400, json: { detail: 'Сравнение этого отчёта уже выполняется.' } });
    return route.fallback();
  });
  const panel = await openNoise(page, true);
  const comparison = panel.getByRole('region', { name: 'Chunk comparison', exact: true });
  await expect(comparison.getByRole('alert')).toHaveText('The database is busy. Try again in a few seconds.');
  await expect(panel.getByRole('button', { name: 'Delete report' })).toBeDisabled();
  unavailable = false;
  await comparison.getByRole('button', { name: 'Compare chunks and tokens' }).click();
  await expect(comparison.getByRole('alert')).toHaveText('A comparison of this report is already running.');
});

test('limited prefix and incomplete passes never claim a full verified difference', async ({ page }) => {
  const state = await mockNoise(page, true);
  state.comparison = readyComparison();
  state.comparison.summary.complete = false; state.comparison.summary.limits = ['message_limit'];
  const panel = await openNoise(page, true);
  const comparison = panel.getByRole('region', { name: 'Chunk comparison', exact: true });
  await expect(comparison.getByText('Compared the first 55 messages.')).toBeVisible();
  await expect(comparison.getByText('The comparison is limited. It does not cover the entire chat.')).toBeVisible();
  state.comparison.summary.comparison_complete = false; state.comparison.summary.source_verified = false;
  state.comparison.summary.delta = null!; state.comparison.summary.after = null!;
  await expect(comparison.getByText('The comparison is incomplete. No verified difference was calculated.')).toBeVisible();
  await expect(comparison.getByText(/Change in chunks:/u)).toHaveCount(0);
});

test('closing the panel ignores held status and reopening reads the latest comparison', async ({ page }) => {
  const state = await mockNoise(page, true);
  state.comparison = readyComparison();
  let release!: () => void;
  state.comparisonGate = new Promise<void>(resolve => { release = resolve; });
  const panel = await openNoise(page);
  await expect.poll(() => state.comparisonCalls).toBeGreaterThan(0);
  await panel.locator(':scope > details > summary').click();
  state.comparison = { ...readyComparison(), state: 'failed', error: 'tokenizer_unavailable', summary: null! };
  release(); state.comparisonGate = null;
  await expect(panel.getByRole('region', { name: 'Сравнение чанкинга', exact: true })).toHaveCount(0);
  await panel.locator(':scope > details > summary').click();
  const comparison = panel.getByRole('region', { name: 'Сравнение чанкинга', exact: true });
  await expect(comparison.getByText('Подготовьте текстовую модель в настройках приложения. Сравнение использует только её локальный токенизатор.')).toBeVisible();
  await expect(comparison.getByText('Сравнение готово', { exact: true })).toHaveCount(0);
});

for (const english of [false, true]) {
  test(`comparison counts fit a narrow ${english ? 'EN' : 'RU'} panel`, async ({ page }) => {
    await page.setViewportSize({ width: 320, height: 700 });
    const state = await mockNoise(page, true); state.comparison = readyComparison();
    const panel = await openNoise(page, english);
    const comparison = panel.getByRole('region', { name: english ? 'Chunk comparison' : 'Сравнение чанкинга', exact: true });
    await expect(comparison.getByRole('table').first()).toBeVisible();
    const geometry = await comparison.evaluate(element => ({ client: element.clientWidth, scroll: element.scrollWidth }));
    expect(geometry.scroll).toBeLessThanOrEqual(geometry.client + 1);
  });
}
