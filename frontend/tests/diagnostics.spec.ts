import { expect, test } from './fixtures';

const ready = { database_check: 'ok', messages: 123, pending_index_segments: 7, ram_available_bytes: 2 * 1024 ** 3, disk_free_bytes: 3 * 1024 ** 3, diagnostic_snapshot: { state: 'ready', partial: false } };

test('partial doctor snapshots show unknown values and poll once until ready', async ({ page }) => {
  let calls = 0;
  let release!: () => void;
  const held = new Promise<void>(resolve => { release = resolve; });
  await page.route('**/api/doctor', async route => {
    if (++calls === 1) return route.fulfill({ json: { database_check: 'checking', diagnostic_snapshot: { state: 'collecting', partial: true } } });
    await held;
    await route.fulfill({ json: ready });
  });
  await page.goto('/');
  await page.getByRole('button', { name: 'Настройки', exact: true }).click();
  await page.getByRole('tab', { name: 'База', exact: true }).click();
  const dialog = page.getByRole('dialog');
  await expect(dialog.getByText('Собираем подробную диагностику…', { exact: true })).toBeVisible();
  await expect(dialog.locator('.diagnostics').first()).not.toContainText(/undefined|NaN|Требует проверки/);
  await expect(dialog.locator('.diagnostics dd').filter({ hasText: /^—$/ })).toHaveCount(4);
  await expect.poll(() => calls).toBe(2);
  await expect(dialog.getByRole('button', { name: 'Обновить диагностику', exact: true })).toBeDisabled();
  await page.waitForTimeout(2200);
  expect(calls).toBe(2);
  release();
  await expect(dialog.locator('.diagnostics').first()).toContainText('Исправна');
  await expect(dialog.locator('.diagnostics').first()).toContainText('123');
  await expect(dialog.locator('.diagnostics').first()).toContainText('2.0 ГиБ');
  await expect(dialog.getByText('Собираем подробную диагностику…', { exact: true })).toHaveCount(0);
  await page.waitForTimeout(2200);
  expect(calls).toBe(2);
});

test('failed diagnostics can be refreshed and closing discards a delayed reply', async ({ page }) => {
  let calls = 0;
  let release!: () => void;
  const held = new Promise<void>(resolve => { release = resolve; });
  await page.route('**/api/doctor', async route => {
    calls++;
    if (calls === 1) return route.fulfill({ json: { database_check: 'checking', diagnostic_snapshot: { state: 'failed', partial: true } } });
    if (calls === 2) await held;
    await route.fulfill({ json: ready }).catch(() => {});
  });
  await page.goto('/');
  await page.getByRole('button', { name: 'Настройки', exact: true }).click();
  await page.getByRole('tab', { name: 'База', exact: true }).click();
  const dialog = page.getByRole('dialog');
  await expect(dialog.getByText('Не удалось собрать подробную диагностику. Повторите позже.', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'RU', exact: true }).press('End');
  await expect(dialog.getByText('Could not collect detailed diagnostics. Try again later.', { exact: true })).toBeVisible();
  await dialog.getByRole('button', { name: 'Refresh diagnostics', exact: true }).click();
  await expect.poll(() => calls).toBe(2);
  await page.getByRole('button', { name: 'Close', exact: true }).click();
  release();
  await page.waitForTimeout(2200);
  await expect(page.getByRole('dialog')).toHaveCount(0);
  expect(calls).toBe(2);
  await page.getByRole('button', { name: 'Settings', exact: true }).click();
  await expect(page.getByRole('dialog').locator('.diagnostics').first()).toContainText('123');
  await expect(page.getByRole('dialog').locator('.diagnostics').first()).not.toContainText(/undefined|NaN/);
});

test('waiting diagnostics stop automatic polling at its bound and allow manual retry', async ({ page }) => {
  await page.clock.install();
  let calls = 0;
  await page.route('**/api/doctor', route => {
    calls++;
    return route.fulfill({ json: { database_check: 'checking', diagnostic_snapshot: { state: 'waiting', partial: true } } });
  });
  await page.goto('/');
  await page.getByRole('button', { name: 'Настройки', exact: true }).click();
  await page.getByRole('tab', { name: 'База', exact: true }).click();
  const dialog = page.getByRole('dialog');
  await expect.poll(() => calls).toBe(1);
  for (let count = 2; count <= 15; count++) {
    await page.clock.runFor(2000);
    await expect.poll(() => calls).toBe(count);
    await expect(dialog.getByRole('button', { name: 'Обновить диагностику', exact: true })).toBeEnabled();
  }
  await expect(dialog.getByText('Диагностика ещё собирается. Обновите сведения позже.', { exact: true })).toBeVisible();
  await page.clock.runFor(10000);
  expect(calls).toBe(15);
  await dialog.getByRole('button', { name: 'Обновить диагностику', exact: true }).click();
  await expect.poll(() => calls).toBe(16);
});
