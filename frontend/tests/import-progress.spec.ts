import { expect, test } from './fixtures';

const progress = { phase: 'metadata', completed: 12 * 1024 ** 2, total: 100 * 1024 ** 2,
  unit: 'bytes', elapsed_seconds: 12, phase_elapsed_seconds: 12, rate: null, eta_seconds: null, activity: null };
const preview = { id: 'synthetic-preview', chat_name: '', scope: 'synthetic', state: 'running',
  operation: 'preview', preparing: true, root_relative_path: '../synthetic', json_relative_path: 'result.json',
  processed: 0, added: 0, unchanged: 0, updated: 0, conflicts: 0, missing_media: 0, invalid_media: 0,
  warnings: [], error: null, progress, applied_import_id: null as string | null };

for (const english of [false, true]) {
  test(`preparation is visible, survives closing and resumes the same report (${english ? 'EN' : 'RU'})`, async ({ page }) => {
    if (english) await page.addInitScript(() => localStorage.setItem('bts.language', 'en'));
    let created = false;
    let status = structuredClone(preview);
    const ids: string[] = [];
    await page.route('**/api/import-previews', route => {
      if (route.request().method() === 'POST') { created = true; return route.fulfill({ json: status }); }
      return route.fulfill({ json: created ? [status] : [] });
    });
    await page.route('**/api/import-previews/synthetic-preview', route => {
      ids.push('synthetic-preview'); return route.fulfill({ json: status });
    });
    await page.route('**/api/import-previews/synthetic-preview/pause', route => {
      status = { ...status, state: 'paused' }; return route.fulfill({ json: status });
    });
    await page.route('**/api/import-previews/synthetic-preview/resume', route => {
      status = { ...status, state: 'running', preparing: false, chat_name: 'Synthetic archive', processed: 50,
        progress: { ...progress, phase: 'checking_messages', unit: 'messages', completed: 50, total: 200 } };
      return route.fulfill({ json: status });
    });
    await page.goto('/');
    await page.getByRole('button', { name: english ? 'Import messages' : 'Импорт сообщений', exact: true }).click();
    await page.getByLabel(english ? 'JSON file path' : 'Путь к JSON', { exact: true }).fill('/synthetic/result.json');
    await page.getByRole('button', { name: english ? 'Check export' : 'Проверить экспорт', exact: true }).click();
    let dialog = page.getByRole('dialog');
    const phase = english ? 'Reading metadata and hashing JSON' : 'Чтение метаданных и хеширование JSON';
    await expect(dialog.getByText(phase, { exact: true })).toBeVisible();
    const barName = english ? 'Import phase progress' : 'Прогресс этапа импорта';
    await expect(dialog.getByRole('progressbar', { name: barName, exact: true })).toHaveAttribute('value', String(progress.completed));
    await expect(dialog.getByText(/До конца этапа:|remaining in phase/)).toHaveCount(0);
    await page.getByRole('button', { name: english ? 'Close' : 'Закрыть', exact: true }).click();
    await expect(page.locator('.jobs .import-progress').filter({ hasText: phase })).toHaveCount(1);
    await page.getByRole('button', { name: english ? 'Open report' : 'Открыть отчёт', exact: true }).click();
    dialog = page.getByRole('dialog');
    await expect(dialog.getByText(phase, { exact: true })).toBeVisible();
    await dialog.getByRole('button', { name: english ? 'Pause' : 'Пауза', exact: true }).click();
    await expect(dialog.getByText(english ? 'Paused' : 'На паузе', { exact: true })).toBeVisible();
    await dialog.getByRole('button', { name: english ? 'Resume check' : 'Продолжить проверку', exact: true }).click();
    await expect(dialog.getByRole('progressbar', { name: barName, exact: true })).toHaveAttribute('value', '50');
    status = { ...status, state: 'ready', processed: 200, added: 200,
      progress: { ...status.progress, phase: 'preview_ready', completed: 200 } };
    await expect(dialog.getByRole('heading', { name: english ? 'Report ready' : 'Отчёт готов', exact: true })).toBeVisible();
    await expect(dialog.getByText(english ? 'The check is complete. Apply the report to import messages.' : 'Проверка завершена. Для записи сообщений примените отчёт.', { exact: true })).toBeVisible();
    expect(new Set(ids)).toEqual(new Set(['synthetic-preview']));
  });
}

test('background apply stays distinct from verification and its import report can be reopened', async ({ page }) => {
  let status = { ...structuredClone(preview), state: 'ready', preparing: false,
    chat_name: 'Synthetic archive', processed: 200, added: 200,
    progress: { ...progress, phase: 'preview_ready', unit: 'messages', completed: 200, total: 200 } };
  let job = { id: 'synthetic-import', chat_name: 'Synthetic archive', state: 'running', processed: 25, added: 25,
    updated: 0, unchanged: 0, conflicts: 0, missing_media: 0, invalid_media: 0, pending_conflicts: 0,
    warnings: [], error: null, progress: { ...progress, phase: 'writing_messages', unit: 'messages', completed: 25, total: 200 } };
  await page.route('**/api/import-previews', route => route.fulfill({ json: status.state === 'applied' ? [] : [status] }));
  await page.route('**/api/import-previews/synthetic-preview', route => route.fulfill({ json: status }));
  await page.route('**/api/import-previews/synthetic-preview/apply?background=true', route => {
    status = { ...status, operation: 'apply', state: 'running', progress: { ...progress, phase: 'verify_json' } };
    return route.fulfill({ json: status });
  });
  await page.route('**/api/imports', route => route.fulfill({ json: status.state === 'applied' ? [job] : [] }));
  await page.route('**/api/imports/synthetic-import', route => route.fulfill({ json: job }));
  await page.goto('/');
  await page.getByRole('button', { name: 'Открыть отчёт', exact: true }).click();
  await page.getByRole('button', { name: 'Применить изменения', exact: true }).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await page.getByRole('button', { name: 'Открыть отчёт', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Применение отчёта', exact: true })).toBeVisible();
  await expect(page.getByRole('dialog').getByText('Проверка неизменности JSON', { exact: true })).toBeVisible();
  status = { ...status, state: 'applied', applied_import_id: job.id };
  const dialog = page.getByRole('dialog');
  await expect(dialog.getByText('Запись сообщений', { exact: true })).toBeVisible();
  await expect(dialog.getByRole('progressbar', { name: 'Прогресс этапа импорта', exact: true })).toHaveAttribute('value', '25');
  job = { ...job, state: 'completed', processed: 200, added: 200,
    progress: { ...job.progress, phase: 'import_completed', completed: 200 } };
  await expect(dialog.getByText('Импорт завершён. Сообщения доступны для поиска по словам; смысловые индексы готовятся отдельно.', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Закрыть', exact: true }).click();
  await page.getByRole('button', { name: 'Открыть импорт', exact: true }).click();
  await expect(page.getByRole('dialog').getByText('Импорт завершён', { exact: true })).toBeVisible();
  await expect(page.getByRole('dialog').getByText('Отчёт импорта', { exact: true })).toBeVisible();
});

test('legacy completed reports without progress do not look queued or active', async ({ page }) => {
  const ready = { ...preview, chat_name: 'Synthetic archive', state: 'ready', preparing: false,
    processed: 200, progress: null };
  const completed = { id: 'legacy-import', chat_name: 'Synthetic archive', state: 'completed',
    processed: 200, added: 200, updated: 0, unchanged: 0, conflicts: 0,
    missing_media: 0, invalid_media: 0, pending_conflicts: 0, warnings: [], error: null, progress: null };
  await page.route('**/api/import-previews', route => route.fulfill({ json: [ready] }));
  await page.route('**/api/import-previews/synthetic-preview', route => route.fulfill({ json: ready }));
  await page.route('**/api/imports', route => route.fulfill({ json: [completed] }));
  await page.route('**/api/imports/legacy-import', route => route.fulfill({ json: completed }));
  await page.goto('/');
  await page.getByRole('button', { name: 'Открыть отчёт', exact: true }).click();
  await expect(page.getByRole('dialog').getByText('Проверка завершена', { exact: true })).toBeVisible();
  await expect(page.getByRole('dialog').getByRole('progressbar')).toHaveCount(0);
  await page.getByRole('button', { name: 'Закрыть', exact: true }).click();
  await page.getByRole('button', { name: 'Открыть импорт', exact: true }).click();
  await expect(page.getByRole('dialog').getByText('Импорт завершён', { exact: true })).toBeVisible();
  await expect(page.getByRole('dialog').getByRole('progressbar')).toHaveCount(0);
  await expect(page.getByRole('dialog')).not.toContainText('Ожидание в очереди');
});

test('accepted apply closes while the archive list refresh is held', async ({ page }) => {
  let status = { ...preview, chat_name: 'Synthetic archive', state: 'ready', preparing: false };
  let hold = false, entered = false;
  let release: () => void = () => {};
  const waiting = new Promise<void>(resolve => { release = resolve; });
  await page.route('**/api/chats', async route => {
    if (hold) { entered = true; await waiting; }
    await route.continue();
  });
  await page.route('**/api/import-previews', route => route.fulfill({ json: [status] }));
  await page.route('**/api/import-previews/synthetic-preview', route => route.fulfill({ json: status }));
  await page.route('**/api/import-previews/synthetic-preview/apply?background=true', route => {
    hold = true; status = { ...status, state: 'queued', operation: 'apply' };
    return route.fulfill({ status: 202, json: status });
  });
  try {
    await page.goto('/');
    await page.getByRole('button', { name: 'Открыть отчёт', exact: true }).click();
    await page.getByRole('button', { name: 'Применить изменения', exact: true }).click();
    await expect.poll(() => entered).toBe(true);
    await expect(page.getByRole('dialog')).toHaveCount(0);
    await page.getByRole('button', { name: 'Настройки', exact: true }).click();
    await expect(page.getByRole('heading', { name: 'Настройки', exact: true })).toBeVisible();
  } finally { release(); }
});

test('import controls and local polling stay available during a held sidebar refresh', async ({ page }) => {
  let job = { id: 'controlled-import', chat_name: 'Synthetic archive', state: 'running', processed: 25, added: 25,
    updated: 0, unchanged: 0, conflicts: 0, missing_media: 0, invalid_media: 0, pending_conflicts: 0,
    warnings: [], error: null, progress: { ...progress, phase: 'writing_messages', unit: 'messages', completed: 25, total: 200 } };
  let hold = false, entered = false, reads = 0;
  let release: () => void = () => {};
  const waiting = new Promise<void>(resolve => { release = resolve; });
  await page.route('**/api/chats', async route => {
    if (hold) { entered = true; await waiting; }
    await route.continue();
  });
  await page.route('**/api/imports', route => route.fulfill({ json: [job] }));
  await page.route('**/api/imports/controlled-import', route => { reads++; return route.fulfill({ json: job }); });
  await page.route('**/api/imports/controlled-import/pause', route => {
    hold = true; job = { ...job, state: 'paused' }; return route.fulfill({ json: job });
  });
  await page.route('**/api/imports/controlled-import/resume', route => {
    job = { ...job, state: 'running' }; return route.fulfill({ json: job });
  });
  try {
    await page.goto('/');
    await page.getByRole('button', { name: 'Открыть импорт', exact: true }).click();
    const dialog = page.getByRole('dialog');
    await dialog.getByRole('button', { name: 'Пауза', exact: true }).click();
    await expect.poll(() => entered).toBe(true);
    await expect(dialog.getByRole('button', { name: 'Продолжить', exact: true })).toBeEnabled();
    const before = reads;
    await dialog.getByRole('button', { name: 'Продолжить', exact: true }).click();
    await expect(dialog.getByRole('button', { name: 'Пауза', exact: true })).toBeEnabled();
    await expect.poll(() => reads).toBeGreaterThan(before);
  } finally { release(); }
});
