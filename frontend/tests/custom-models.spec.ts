import { test, expect } from './fixtures';

test.afterEach(async ({ page }) => { await page.unrouteAll({ behavior: 'wait' }); });

for (const english of [false, true]) {
  test(`custom model code runs only after explicit inspect and validate (${english ? 'EN' : 'RU'})`, async ({ page }) => {
    if (english) await page.addInitScript(() => localStorage.setItem('bts.language', 'en'));
    const identity = 'a'.repeat(64);
    const summary = { id: 'public-example', role: 'text_embedding', identity, runtime: { name: 'python', version: '3' } };
    let registered = false; let validations = 0; let inspect = 0;
    await page.route('**/api/adapters', route => route.fulfill({ json: { models: registered ? [summary] : [], errors: [], job: null } }));
    await page.route('**/api/semantic', async route => {
      const response = await route.fetch(); const status = await response.json();
      await route.fulfill({ json: { ...status, profiles: [...status.profiles, ...(registered ? [{ profile: 'custom:' + identity, model_id: 'custom/public-example', dimension: 4, download_bytes: 0 }] : [])] } });
    });
    await page.route('**/api/adapters/jobs', route => {
      const body = route.request().postDataJSON();
      if (body.action === 'inspect') {
        inspect++; return route.fulfill({ status: 202, json: { id: 'i'.repeat(32), state: 'inspected', action: 'inspect', role: 'text_embedding', report: summary, progress: null, error: null } });
      }
      validations++; expect(body).toMatchObject({ action: 'validate', expected_identity: identity, python: '/synthetic/venv/python', device: 'cpu' });
      registered = true;
      return route.fulfill({ status: 202, json: { id: 'v'.repeat(32), state: 'passed', action: 'validate', role: 'text_embedding', report: { ...summary, registered: true }, progress: null, error: null } });
    });
    await page.goto('/');
    await page.getByRole('button', { name: english ? 'Settings' : 'Настройки', exact: true }).click();
    await page.getByRole('tab', { name: english ? 'Models' : 'Модели', exact: true }).click();
    await page.getByText(english ? 'Add your own model' : 'Добавить свою модель', { exact: true }).click();
    await page.getByLabel(english ? 'Adapter manifest path' : 'Путь к manifest адаптера').fill('/synthetic/manifest.json');
    await page.getByLabel(english ? 'Python in the separate environment' : 'Python отдельного окружения').fill('/synthetic/venv/python');
    const validate = page.getByRole('button', { name: english ? 'Run check and add' : 'Запустить проверку и добавить', exact: true });
    await expect(validate).toBeDisabled(); expect(validations).toBe(0); expect(inspect).toBe(0);
    await page.getByRole('button', { name: english ? 'Check manifest and files' : 'Проверить manifest и файлы', exact: true }).click();
    await expect(validate).toBeEnabled(); expect(validations).toBe(0); expect(inspect).toBe(1);
    await validate.click();
    await expect(page.getByText(english ? 'Model added. Select it in the text card; changing the index requires confirmation.' : 'Модель добавлена. Выберите её в карточке текста; смена индекса требует подтверждения.', { exact: true })).toBeVisible();
    const model = page.getByLabel(english ? 'Semantic search model' : 'Модель смыслового поиска', { exact: true });
    await expect(model.locator(`option[value="custom:${identity}"]`)).toHaveText('custom/public-example');
    await expect(model).not.toHaveValue('custom:' + identity);
    await page.getByLabel(english ? 'Adapter manifest path' : 'Путь к manifest адаптера').fill('/synthetic/other.json');
    await expect(validate).toBeDisabled();
    expect(validations).toBe(1);
  });
}

test('a running adapter check remains cancellable after a transient status failure', async ({ page }) => {
  const job = { id: 'a'.repeat(32), state: 'running', action: 'validate', role: 'text_embedding', report: null, error: null, progress: null };
  await page.route('**/api/adapters', route => route.fulfill({ json: { models: [], errors: [], job } }));
  await page.route('**/api/adapters/jobs/*', route => route.fulfill({ status: 503, json: { detail: 'Temporary check failure' } }));
  let cancelled = false;
  await page.route('**/api/adapters/jobs/*/cancel', route => { cancelled = true; return route.fulfill({ json: { ...job, state: 'cancelled' } }); });
  await page.goto('/');
  await page.getByRole('button', { name: 'Настройки', exact: true }).click();
  await page.getByRole('tab', { name: 'Модели', exact: true }).click();
  await page.getByText('Добавить свою модель', { exact: true }).click();
  await expect(page.getByText('Temporary check failure', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Отменить проверку', exact: true }).click();
  await expect(page.getByText('Проверка отменена.', { exact: true })).toBeVisible();
  expect(cancelled).toBe(true);
});

test('retry repeats registration refresh of both model settings and semantic choices', async ({ page }) => {
  const identity = 'b'.repeat(64);
  const summary = { id: 'public-retry', role: 'text_embedding', identity, runtime: { name: 'python', version: '3' } };
  const job = { id: 'c'.repeat(32), state: 'passed', action: 'validate', role: 'text_embedding', report: { ...summary, registered: true }, progress: null, error: null };
  let reads = 0; let fail = true; let exposeCustom = false;
  await page.route('**/api/adapters', route => route.fulfill({ json: { models: [summary], errors: [], job } }));
  await page.route('**/api/models', async route => {
    reads++;
    if (reads > 1 && fail) { fail = false; return route.fulfill({ status: 503, json: { detail: 'Model paths unavailable' } }); }
    return route.continue();
  });
  await page.route('**/api/semantic', async route => {
    const response = await route.fetch(); const status = await response.json();
    await route.fulfill({ json: { ...status, profiles: [...status.profiles, ...(exposeCustom ? [{ profile: 'custom:' + identity, model_id: 'custom/public-retry', dimension: 4, download_bytes: 0 }] : [])] } });
  });
  await page.goto('/');
  await page.getByRole('button', { name: 'Настройки', exact: true }).click();
  await page.getByRole('tab', { name: 'Модели', exact: true }).click();
  await page.getByText('Добавить свою модель', { exact: true }).click();
  await expect(page.getByText('Model paths unavailable', { exact: true })).toBeVisible();
  const before = reads;
  exposeCustom = true;
  await page.locator('.custom-models').getByRole('button', { name: 'Повторить', exact: true }).click();
  await expect(page.getByText('Model paths unavailable', { exact: true })).toHaveCount(0);
  await expect.poll(() => reads).toBeGreaterThan(before);
  await expect(page.getByLabel('Модель смыслового поиска', { exact: true }).locator(`option[value="custom:${identity}"]`)).toHaveText('custom/public-retry');
});

for (const language of ['ru', 'en']) for (const theme of ['light', 'dark']) {
  test(`long adapter metadata fits and known errors translate (${language}/${theme})`, async ({ page }) => {
    await page.setViewportSize({ width: 320, height: 700 });
    await page.addInitScript(({ language, theme }) => { localStorage.setItem('bts.language', language); localStorage.setItem('bts.theme', theme); }, { language, theme });
    const summary = { id: 'b'.repeat(64), role: 'text_embedding', identity: 'a'.repeat(64), runtime: { name: 'python', version: '3' } };
    await page.route('**/api/adapters', route => route.fulfill({ json: { models: [summary], errors: ['The adapter role differs from the selected role.'], job: { id: 'f'.repeat(32), state: 'inspected', action: 'inspect', role: 'text_embedding', report: summary, error: null, progress: null } } }));
    const english = language === 'en';
    await page.goto('/');
    await page.getByRole('button', { name: english ? 'Settings' : 'Настройки', exact: true }).click();
    await page.getByRole('tab', { name: english ? 'Models' : 'Модели', exact: true }).click();
    await page.getByText(english ? 'Add your own model' : 'Добавить свою модель', { exact: true }).click();
    await expect(page.getByText(english ? 'The adapter role differs from the selected role.' : 'Роль адаптера отличается от выбранной роли.', { exact: true })).toBeVisible();
    expect(await page.getByRole('dialog').locator('.modal').evaluate(element => element.scrollWidth <= element.clientWidth + 1)).toBe(true);
  });
}
