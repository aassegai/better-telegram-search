import { expect, test } from './fixtures';
import { mockSearchJobs } from './search-mocks';

test('ANN choice is independent per tab, persists and never starts work on its own', async ({ page }) => {
  const work: string[] = [];
  page.on('request', request => {
    const path = new URL(request.url()).pathname;
    if (request.method() === 'POST' && (path.includes('/jobs') || path.includes('/prepare') || path.includes('/ann-indexes'))) work.push(path);
  });
  await page.goto('/');
  const choice = page.getByRole('group', { name: 'Полнота поиска', exact: true });
  const ann = choice.getByRole('button', { name: 'Быстрее · ANN', exact: true });
  const full = choice.getByRole('button', { name: 'Полнее · полный поиск', exact: true });
  await expect(full).toHaveAttribute('aria-pressed', 'true');
  await expect(ann).toBeDisabled();
  await expect(choice).toContainText('Просмотр сообщений не использует ANN.');
  await page.getByLabel('Поисковый запрос', { exact: true }).fill('ANN draft');
  await page.getByLabel('Режим поиска', { exact: true }).selectOption('words');
  await page.getByRole('checkbox', { name: 'Изображения', exact: true }).uncheck();
  await expect(ann).toBeDisabled();
  await expect(choice).toContainText('Поиск только по словам не использует ANN.');
  await page.getByRole('checkbox', { name: 'Изображения', exact: true }).check();
  await expect(ann).toBeEnabled(); // Image descriptions are semantic in every non-phrase mode.
  await page.getByLabel('Точная фраза', { exact: true }).check();
  await expect(ann).toBeDisabled();
  await expect(choice).toContainText('Точная фраза не использует ANN.');
  await page.getByLabel('Точная фраза', { exact: true }).uncheck();
  await ann.click();
  await expect(ann).toHaveAttribute('aria-pressed', 'true');
  await page.getByRole('button', { name: 'Новая вкладка поиска', exact: true }).click();
  await expect(full).toHaveAttribute('aria-pressed', 'true');
  await page.getByRole('tab', { name: /^ANN draft/ }).click();
  await expect(ann).toHaveAttribute('aria-pressed', 'true');
  await page.reload();
  await expect(ann).toHaveAttribute('aria-pressed', 'true');
  await page.getByRole('button', { name: 'RU', exact: true }).press('End');
  await expect(page.getByRole('group', { name: 'Search coverage', exact: true })).toContainText('ANN may miss close matches.');
  await expect(page.getByRole('button', { name: 'Faster · ANN', exact: true })).toHaveAttribute('aria-pressed', 'true');
  expect(work).toEqual([]);
});

test('submitted strategy and fallback stay attached to results while the draft changes', async ({ page }) => {
  const specs: { strategy: string }[] = [];
  await mockSearchJobs(page, route => {
    specs.push(route.request().postDataJSON());
    return route.fulfill({ json: { results: [], effective_mode: 'meaning', warnings: [], has_more: false,
      requested_strategy: specs.at(-1)!.strategy, effective_strategy: specs.length === 1 ? 'exact' : 'ann',
      fallback_reasons: specs.length === 1 ? ['ann_index_unavailable'] : [],
    } });
  });
  await page.goto('/');
  await page.getByLabel('Поисковый запрос', { exact: true }).fill('semantic query');
  await page.getByLabel('Режим поиска', { exact: true }).selectOption('meaning');
  await page.getByRole('button', { name: 'Быстрее · ANN', exact: true }).click();
  await page.getByRole('button', { name: 'Найти', exact: true }).click();
  const result = page.locator('.results .retrieval-summary');
  await expect(result).toContainText('Выполнен полный поиск');
  await expect(result).toContainText('Индекс ANN ещё не подготовлен.');
  await page.getByRole('button', { name: 'Полнее · полный поиск', exact: true }).click();
  await expect(result).toContainText('Индекс ANN ещё не подготовлен.');
  expect(specs).toHaveLength(1);
  await page.getByLabel('Поисковый запрос', { exact: true }).fill('another query');
  await page.getByRole('button', { name: 'Быстрее · ANN', exact: true }).click();
  await page.getByRole('button', { name: 'Найти', exact: true }).click();
  await expect(result).toHaveText('Выполнен поиск ANN');
  expect(specs.map(spec => spec.strategy)).toEqual(['ann', 'ann']);
  await page.getByRole('button', { name: 'Полнее · полный поиск', exact: true }).click();
  await expect(result).toHaveText('Выполнен поиск ANN');
  await page.getByRole('button', { name: 'RU', exact: true }).press('End');
  await expect(result).toHaveText('ANN search was used');
});

test('native ANN progress has unknown totals, measured calls and cancellable work', async ({ page }) => {
  let state = 'running', phase = 'image_vectors_scope', unit = 'ids', completed = 512;
  const status = () => ({ job_id: 'native-ann', state, phase, unit, completed,
    total: null, elapsed_seconds: 12, rate: null, eta_seconds: null,
    requested_strategy: 'ann', effective_strategy: 'none', fallback_reasons: [],
    queue_position: null, warnings: [], error: null });
  await page.route('**/api/search-tabs/*/jobs', route => route.fulfill({ json: status() }));
  await page.route('**/api/search-jobs/native-ann', route => route.fulfill({ json: status() }));
  await page.route('**/api/search-jobs/native-ann/cancel', route => {
    state = 'cancelled'; return route.fulfill({ json: status() });
  });
  await page.goto('/');
  await page.getByLabel('Поисковый запрос', { exact: true }).fill('image description');
  await page.getByRole('button', { name: 'Быстрее · ANN', exact: true }).click();
  await page.getByRole('button', { name: 'Найти', exact: true }).click();
  const progress = page.getByRole('region', { name: 'Выполнение поиска', exact: true });
  await expect(progress).toContainText('Проверка области ANN изображений');
  await expect(progress).toContainText('Проверено записей: 512');
  await expect(progress.getByRole('progressbar')).not.toHaveAttribute('value');
  await expect(progress).not.toContainText('До конца этапа');
  phase = 'image_vectors'; unit = 'native_queries'; completed = 3;
  await expect(progress).toContainText('Завершено вызовов ANN: 3');
  await expect(progress.getByRole('progressbar')).not.toHaveAttribute('value');
  await page.getByRole('button', { name: 'Остановить', exact: true }).click();
  await expect(page.getByText('Поиск остановлен', { exact: true })).toBeVisible();
  await expect(progress).toHaveCount(0);
});

const space = 'a'.repeat(64);
const statusFixture = () => ({ available: true, enabled: false, initializing: false,
  running: null as string | null, error: null as string | null,
  resources: { threads: 2, memory_limit_mib: 1024 }, entries: [{ space_id: space,
    roles: ['text', 'ocr'], dimension: 768, metric: 'cosine', rows: 4096,
    state: 'not_prepared', ready: false, needs_update: true, indexed_rows: 0,
    unindexed_rows: 4096, error: null as string | null, elapsed_seconds: 0 }],
});

test('ANN performance saves only ANN edits, prepares once, stops and exposes retry errors', async ({ page }) => {
  let settings = await (await page.request.get('/api/settings')).json();
  let status = statusFixture();
  const writes: unknown[] = [], prepares: unknown[] = [];
  let cancels = 0;
  await page.route('**/api/settings', route => {
    if (route.request().method() === 'PATCH') {
      const body = route.request().postDataJSON(); writes.push(body); settings = { ...settings, ...body };
      status.resources = { threads: settings.ann_build_threads, memory_limit_mib: settings.ann_memory_limit_mib };
    }
    return route.fulfill({ json: settings });
  });
  await page.route('**/api/ann-indexes', route => route.fulfill({ json: status }));
  await page.route('**/api/ann-indexes/prepare', route => {
    prepares.push(route.request().postDataJSON()); status.entries[0].state = 'queued';
    return route.fulfill({ status: 202, json: status });
  });
  await page.route('**/api/ann-indexes/cancel', route => {
    cancels++; status.enabled = false; settings.ann_indexing_enabled = false;
    status.entries[0].state = 'cancelling';
    return route.fulfill({ json: status });
  });
  await page.goto('/');
  await page.getByRole('button', { name: 'Настройки', exact: true }).click();
  const panel = page.getByRole('region', { name: 'Ускорение поиска · ANN', exact: true });
  const entry = panel.getByRole('region', { name: 'ANN · Текст и OCR', exact: true });
  const save = panel.getByRole('button', { name: 'Сохранить настройки ANN', exact: true });
  await expect(entry).toContainText('Векторов: 4');
  await expect(save).toBeDisabled();
  await panel.getByLabel('Потоков построения ANN', { exact: true }).fill('0');
  await expect(save).toBeDisabled();
  await panel.getByLabel('Потоков построения ANN', { exact: true }).fill('1');
  await panel.getByLabel('Память ANN (МиБ)', { exact: true }).fill('512');
  await panel.getByText('Расширенные параметры ANN', { exact: true }).click();
  await panel.getByLabel('Проверяемых разделов ANN', { exact: true }).fill('8');
  await expect(entry.getByRole('button', { name: 'Подготовить ANN', exact: true })).toBeDisabled();
  await save.click();
  await expect(panel.getByText('Настройки ANN сохранены.', { exact: true })).toBeVisible();
  expect(writes).toEqual([{ ann_build_threads: 1, ann_memory_limit_mib: 512, ann_probes: 8 }]);
  await entry.getByRole('button', { name: 'Подготовить ANN', exact: true }).click();
  await expect(entry).toContainText('В очереди');
  await expect(entry.getByRole('button', { name: 'Подготовить ANN', exact: true })).toBeDisabled();
  expect(prepares).toEqual([{ space_id: space }]);
  status = { ...status, running: 'build', entries: [{ ...status.entries[0], state: 'building', elapsed_seconds: 8 }] };
  const progress = entry.getByRole('progressbar');
  await expect(progress).toBeVisible();
  await expect(progress).not.toHaveAttribute('value');
  await expect(entry).toContainText('Прошло 8 с');
  await panel.getByLabel('Потоков построения ANN', { exact: true }).fill('3');
  await panel.getByRole('button', { name: 'Остановить ANN', exact: true }).click();
  await expect(panel).toContainText('Остановка ANN запрошена; автоматическое построение отключено.');
  await expect(panel.getByLabel('Потоков построения ANN', { exact: true })).toHaveValue('3');
  await expect(entry).toContainText('Останавливаем…');
  await expect(progress).toBeVisible();
  expect(cancels).toBe(1);
  status = { ...status, running: null, entries: [{ ...status.entries[0], state: 'failed', error: 'ann_memory_limit' }] };
  await expect(entry.getByRole('alert')).toContainText('Достигнут лимит памяти ANN.');
  await panel.getByLabel('Потоков построения ANN', { exact: true }).fill('1');
  await entry.getByRole('button', { name: 'Повторить построение ANN', exact: true }).click();
  await expect(entry).toContainText('В очереди');
  expect(prepares).toEqual([{ space_id: space }, { space_id: space }]);
  status = { ...status, running: null, entries: [{ ...status.entries[0], state: 'ready',
    ready: true, needs_update: false, indexed_rows: 4096, error: null }] };
  await page.getByRole('dialog').getByRole('button', { name: 'EN', exact: true }).click();
  await expect(page.getByRole('region', { name: 'Search acceleration · ANN', exact: true })).toBeVisible();
  await expect(page.getByRole('region', { name: 'ANN · Text and OCR', exact: true }).locator('.model-state')).toHaveText('Ready');
  await expect(page.getByRole('button', { name: 'Save ANN settings', exact: true })).toBeDisabled();
});

test('ANN polling cannot overwrite an accepted action with a delayed status reply', async ({ page }) => {
  let release!: () => void;
  let held = false, replied = false;
  let status = statusFixture();
  let reads = 0;
  await page.route('**/api/ann-indexes', async route => {
    reads++;
    const captured = structuredClone(status);
    if (held) {
      await new Promise<void>(resolve => { release = resolve; });
      replied = true;
    }
    await route.fulfill({ json: captured });
  });
  await page.route('**/api/ann-indexes/prepare', route => {
    status = { ...status, running: 'new-build', entries: [{ ...status.entries[0], state: 'building' }] };
    return route.fulfill({ status: 202, json: status });
  });
  await page.goto('/');
  await page.getByRole('button', { name: 'Настройки', exact: true }).click();
  const panel = page.getByRole('region', { name: 'Ускорение поиска · ANN', exact: true });
  const prepare = panel.getByRole('button', { name: 'Подготовить ANN', exact: true });
  await expect(prepare).toBeEnabled();
  held = true;
  try {
    await expect.poll(() => !!release).toBe(true);
    const before = reads;
    await page.waitForTimeout(1200);
    expect(reads).toBe(before); // A slow poll cannot spawn an overlapping poll.
    held = false;
    await prepare.click();
    await expect(panel.getByRole('progressbar')).toBeVisible();
    release();
    await expect.poll(() => replied).toBe(true);
    await expect(panel.getByRole('progressbar')).toBeVisible();
    await expect(prepare).toBeDisabled();
  } finally { held = false; release?.(); }
});

test('missing runtime, small tables and failed status offer safe controls and retry', async ({ page }) => {
  const status = statusFixture();
  status.entries[0].state = 'small'; status.entries[0].rows = 20;
  let failed = true;
  await page.route('**/api/ann-indexes', route => route.fulfill(failed
    ? { status: 500, json: { detail: 'Synthetic ANN status error' } } : { json: status }));
  await page.goto('/');
  await page.getByRole('button', { name: 'Настройки', exact: true }).click();
  const panel = page.getByRole('region', { name: 'Ускорение поиска · ANN', exact: true });
  await expect(panel.getByRole('alert')).toHaveText('Synthetic ANN status error');
  failed = false;
  await panel.getByRole('button', { name: 'Повторить проверку ANN', exact: true }).click();
  await expect(panel.getByRole('alert')).toHaveCount(0);
  await expect(panel).toContainText('Векторов пока недостаточно для ANN. Доступен полный поиск.');
  await expect(panel.getByRole('button', { name: 'Подготовить ANN', exact: true })).toHaveCount(0);
  status.available = false;
  await expect(panel).toContainText('Среда векторного поиска недоступна.');
});
