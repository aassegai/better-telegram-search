import { expect, test } from './fixtures';

for (const english of [false, true]) {
  test(`Jina selection is explicit and preparation is separate (${english ? 'EN dark narrow' : 'RU light'})`, async ({ page }) => {
    await page.addInitScript(({ en }) => {
      localStorage.setItem('bts.language', en ? 'en' : 'ru');
      localStorage.setItem('bts.theme', en ? 'dark' : 'light');
    }, { en: english });
    if (english) await page.setViewportSize({ width: 360, height: 760 });
    let status = { profile: 'giga', ready: true, enabled: false, preparation_state: 'ready', error: null,
      path: '/synthetic/models/giga', device: 'cpu', download_bytes: 1024, download_completed_bytes: 0 };
    let downloads = 0;
    const writes: Record<string, unknown>[] = [];
    await page.route('**/api/rerank', route => route.fulfill({ json: status }));
    await page.route('**/api/settings', route => {
      if (route.request().method() !== 'PATCH') return route.continue();
      const body = route.request().postDataJSON(); writes.push(body);
      status = { ...status, profile: body.rerank_profile ?? status.profile,
        enabled: body.giga_rerank_enabled ?? status.enabled, ready: false, preparation_state: 'idle' };
      return route.fulfill({ json: {} });
    });
    await page.route('**/api/rerank/prepare', route => {
      expect(status.profile).toBe('jina-v2');
      expect(route.request().postDataJSON()).toEqual({ offline: false });
      downloads++;
      status = { ...status, ready: true, preparation_state: 'ready', path: '/synthetic/models/jina' };
      return route.fulfill({ json: status });
    });
    await page.goto('/');
    await page.getByRole('button', { name: english ? 'Settings' : 'Настройки', exact: true }).click();
    await page.getByRole('tab', { name: english ? 'Models' : 'Модели', exact: true }).click();
    const selector = page.getByLabel(english ? 'Reranking model' : 'Модель переранжирования', { exact: true });
    // An English interface recommends Jina but must preserve the saved Giga selection.
    await expect(selector).toHaveValue('giga');
    await selector.selectOption('jina-v2');
    const prepare = page.getByRole('button', { name: english ? 'Prepare Jina' : 'Подготовить Jina', exact: true });
    await expect(prepare).toBeDisabled();
    await expect(page.getByText(english ? 'Jina v2 uses CC-BY-NC-4.0 for noncommercial use.'
      : 'Jina v2: лицензия CC-BY-NC-4.0, только некоммерческое использование.')).toBeVisible();
    expect(writes).toEqual([]); expect(downloads).toBe(0);
    await page.getByRole('button', { name: english ? 'Save model settings' : 'Сохранить настройки модели', exact: true }).click();
    await expect(prepare).toBeEnabled();
    expect(writes).toEqual([{ rerank_profile: 'jina-v2' }]); expect(downloads).toBe(0);
    await prepare.click();
    expect(downloads).toBe(1);
    const enabled = page.getByLabel(english ? 'Jina reranking' : 'Переранжирование Jina', { exact: true });
    await expect(enabled).toBeEnabled();
    await enabled.check();
    await page.getByRole('button', { name: english ? 'Save model settings' : 'Сохранить настройки модели', exact: true }).click();
    expect(writes).toEqual([{ rerank_profile: 'jina-v2' }, { giga_rerank_enabled: true }]);
    await expect(selector).toHaveValue('jina-v2');
    const box = await selector.boundingBox();
    expect(box?.x).toBeGreaterThanOrEqual(0);
    expect((box?.x ?? 0) + (box?.width ?? 0)).toBeLessThanOrEqual(english ? 360 : 1280);
    await page.keyboard.press('Escape');
    await page.getByRole('button', { name: english ? 'Settings' : 'Настройки', exact: true }).click();
    await expect(selector).toHaveValue('jina-v2');
  });
}

for (const edited of ['profile', 'enabled']) {
  test(`reranker polling preserves externally changed unedited ${edited === 'profile' ? 'enabled' : 'profile'}`, async ({ page }) => {
    let status = { profile: 'giga', ready: true, enabled: false, preparation_state: 'ready', error: null,
      path: '/synthetic/models/giga', device: 'cpu', download_bytes: 1024, download_completed_bytes: 0 };
    const writes: Record<string, unknown>[] = [];
    let reads = 0;
    await page.route('**/api/rerank', route => { reads++; return route.fulfill({ json: status }); });
    await page.route('**/api/settings', route => {
      if (route.request().method() !== 'PATCH') return route.continue();
      writes.push(route.request().postDataJSON()); return route.fulfill({ json: {} });
    });
    await page.goto('/');
    await page.getByRole('button', { name: 'Настройки', exact: true }).click();
    await page.getByRole('tab', { name: 'Модели', exact: true }).click();
    const selector = page.getByLabel('Модель переранжирования', { exact: true });
    await expect(selector).toHaveValue('giga');
    if (edited === 'profile') await selector.selectOption('jina-v2');
    else await page.getByLabel('Переранжирование Giga', { exact: true }).check();
    const previousReads = reads;
    status = edited === 'profile' ? { ...status, enabled: true }
      : { ...status, profile: 'jina-v2', ready: false, preparation_state: 'idle' };
    await expect.poll(() => reads).toBeGreaterThan(previousReads);
    await expect(page.getByLabel('Переранжирование Jina', { exact: true })).toBeChecked();
    await expect(selector).toHaveValue('jina-v2');
    await page.getByRole('button', { name: 'Сохранить настройки модели', exact: true }).click();
    expect(writes).toEqual(edited === 'profile' ? [{ rerank_profile: 'jina-v2' }] : [{ giga_rerank_enabled: true }]);
  });
}

test('Giga preparation is not displayed as draft Jina progress', async ({ page }) => {
  let status = { profile: 'giga', ready: true, enabled: false, preparation_state: 'ready', error: null,
    path: '/synthetic/models/giga', device: 'cpu', download_bytes: 1024, download_completed_bytes: 0 };
  await page.route('**/api/rerank', route => route.fulfill({ json: status }));
  await page.goto('/');
  await page.getByRole('button', { name: 'Настройки', exact: true }).click();
  await page.getByRole('tab', { name: 'Модели', exact: true }).click();
  await page.getByLabel('Модель переранжирования', { exact: true }).selectOption('jina-v2');
  status = { ...status, ready: false, preparation_state: 'downloading', download_completed_bytes: 512 };
  const card = page.getByRole('region', { name: 'Уточнение результатов · Jina', exact: true });
  await expect(card.getByText('Сейчас подготавливается модель: Giga.', { exact: true })).toBeVisible();
  await expect(card.getByRole('progressbar')).toHaveCount(0);
  await expect(card.getByRole('button', { name: 'Сохранить настройки модели', exact: true })).toBeDisabled();
  await expect(card.getByRole('button', { name: 'Подготовить Jina', exact: true })).toBeDisabled();
});
