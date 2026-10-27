import { expect, test } from './fixtures';

for (const english of [false, true]) {
  test(`custom reranker requires save, prepare and enable (${english ? 'EN dark 320' : 'RU light'})`, async ({ page }) => {
    await page.addInitScript(({ english }) => {
      localStorage.setItem('bts.language', english ? 'en' : 'ru');
      localStorage.setItem('bts.theme', english ? 'dark' : 'light');
    }, { english });
    if (english) await page.setViewportSize({ width: 320, height: 760 });
    const profile = 'custom:' + 'a'.repeat(64);
    const name = 'custom/' + 'b'.repeat(64);
    let status = { profile: 'giga', ready: true, enabled: false, preparation_state: 'ready', error: null,
      profiles: [{ profile: 'giga', model_id: 'Giga' }, { profile, model_id: name, revision: 'public-v1' }],
      path: '/synthetic/models/giga', device: 'cpu', download_bytes: 1024, download_completed_bytes: 0 };
    const writes: Record<string, unknown>[] = [];
    let runs = 0;
    await page.route('**/api/rerank', route => route.fulfill({ json: status }));
    await page.route('**/api/settings', route => {
      if (route.request().method() !== 'PATCH') return route.continue();
      const body = route.request().postDataJSON(); writes.push(body);
      const changed = Boolean(body.rerank_profile && body.rerank_profile !== status.profile);
      status = { ...status, profile: body.rerank_profile ?? status.profile, enabled: body.giga_rerank_enabled ?? status.enabled,
        ready: changed ? false : status.ready, preparation_state: changed ? 'idle' : status.preparation_state };
      return route.fulfill({ json: {} });
    });
    await page.route('**/api/rerank/prepare', route => {
      expect(status.profile).toBe(profile);
      expect(route.request().postDataJSON()).toEqual({ offline: true });
      runs++; status = { ...status, ready: true, preparation_state: 'ready', path: '/synthetic/models/custom-adapters/' + 'a'.repeat(64), download_bytes: 0 };
      return route.fulfill({ json: status });
    });
    await page.goto('/');
    await page.getByRole('button', { name: english ? 'Settings' : 'Настройки', exact: true }).click();
    await page.getByRole('tab', { name: english ? 'Models' : 'Модели', exact: true }).click();
    const selector = page.getByLabel(english ? 'Reranking model' : 'Модель переранжирования', { exact: true });
    await expect(selector).toHaveValue('giga');
    await selector.selectOption(profile);
    const card = page.getByRole('region', { name: english ? 'Custom reranking' : 'Пользовательское переранжирование', exact: true });
    const prepare = card.getByRole('button', { name: english ? 'Prepare adapter' : 'Подготовить адаптер', exact: true });
    await expect(prepare).toBeDisabled();
    const enabled = card.getByLabel(english ? 'Custom reranking' : 'Пользовательское переранжирование', { exact: true });
    await expect(enabled).toBeDisabled();
    expect(writes).toEqual([]); expect(runs).toBe(0);
    await card.getByRole('button', { name: english ? 'Save model settings' : 'Сохранить настройки модели', exact: true }).click();
    await expect(prepare).toBeEnabled(); expect(runs).toBe(0);
    expect(writes).toEqual([{ rerank_profile: profile }]);
    await expect(card.getByText(english ? 'Local Python adapter' : 'Локальный Python-адаптер', { exact: true })).toBeVisible();
    await expect(card.getByText(english ? 'Built-in download' : 'Встроенная загрузка', { exact: true })).toHaveCount(0);
    await prepare.click();
    await expect(enabled).toBeEnabled();
    await enabled.check();
    await card.getByRole('button', { name: english ? 'Save model settings' : 'Сохранить настройки модели', exact: true }).click();
    expect(writes).toEqual([{ rerank_profile: profile }, { giga_rerank_enabled: true }]);
    expect(runs).toBe(1);
    expect(await card.evaluate(element => element.scrollWidth <= element.clientWidth + 1)).toBe(true);
  });
}

test('an unavailable registered reranker can be replaced by a built-in model', async ({ page }) => {
  const profile = 'custom:' + 'c'.repeat(64);
  let status = { profile, ready: false, enabled: false, preparation_state: 'idle', error: null,
    profiles: [{ profile, model_id: profile, available: false }, { profile: 'giga', model_id: 'Giga', available: true }],
    path: '/synthetic/missing', device: 'cpu', download_bytes: 0, download_completed_bytes: 0 };
  await page.route('**/api/rerank', route => route.fulfill({ json: status }));
  await page.route('**/api/settings', route => {
    if (route.request().method() !== 'PATCH') return route.continue();
    expect(route.request().postDataJSON()).toEqual({ rerank_profile: 'giga' });
    status = { ...status, profile: 'giga' }; return route.fulfill({ json: {} });
  });
  await page.goto('/');
  await page.getByRole('button', { name: 'Настройки', exact: true }).click();
  await page.getByRole('tab', { name: 'Модели', exact: true }).click();
  const selector = page.getByLabel('Модель переранжирования', { exact: true });
  await expect(selector).toHaveValue(profile);
  await expect(selector.locator('option:checked')).toContainText('Недоступно');
  await selector.selectOption('giga');
  await page.getByRole('region', { name: 'Уточнение результатов · Giga', exact: true }).getByRole('button', { name: 'Сохранить настройки модели', exact: true }).click();
  await expect(selector).toHaveValue('giga');
  await expect(page.getByRole('button', { name: 'Подготовить Giga', exact: true })).toBeEnabled();
});
