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


for (const english of [false, true]) {
  test(`registered image adapters require explicit image reindex (${english ? 'EN' : 'RU'})`, async ({ page }) => {
    if (english) await page.addInitScript(() => localStorage.setItem('bts.language', 'en'));
    const profile = 'custom:' + 'd'.repeat(64);
    await page.route('**/api/models', async route => {
      const response = await route.fetch(); const models = await response.json();
      await route.fulfill({ json: { ...models, clip: { ...models.clip, profile_names: { [profile]: 'custom/public-rgb · test-v1' }, profile_paths: { ...models.clip.profile_paths, [profile]: ['/synthetic/adapter-snapshot'] } } } });
    });
    await page.route('**/api/media-index', async route => {
      const response = await route.fetch(); const media = await response.json();
      await route.fulfill({ json: { ...media, images_enabled: 1, visual_profile: 'siglip2', images_available: true } });
    });
    let request: Record<string, unknown> | null = null;
    await page.route('**/api/media-index/prepare', async route => {
      request = route.request().postDataJSON();
      await route.fulfill({ status: 202, json: { images_enabled: 1, visual_profile: profile, images_available: true, preparation_state: 'ready' } });
    });
    await page.goto('/');
    await page.getByRole('button', { name: english ? 'Settings' : 'Настройки', exact: true }).click();
    await page.getByRole('tab', { name: english ? 'Models' : 'Модели', exact: true }).click();
    const selector = page.getByRole('combobox', { name: english ? 'Visual model' : 'Визуальная модель', exact: true });
    await expect(selector.locator(`option[value="${profile}"]`)).toHaveText('custom/public-rgb · test-v1');
    await selector.selectOption(profile);
    const change = page.getByRole('button', { name: english ? 'Switch model and rebuild images' : 'Сменить модель и перестроить изображения', exact: true });
    await expect(change).toBeDisabled(); expect(request).toBeNull();
    await page.getByLabel(english ? 'Rebuild the image index when switching models; keep the OCR cache' : 'Перестроить индекс изображений при смене модели; кэш OCR сохранится', { exact: true }).check();
    await expect(change).toBeEnabled();
    await change.click();
    await expect.poll(() => request).toEqual({ kind: 'images', offline: false, profile, reindex: true });
  });
}

for (const english of [false, true]) {
  test(`custom OCR selection stays separate from explicit preparation (${english ? 'EN' : 'RU'})`, async ({ page }) => {
    if (english) await page.addInitScript(() => localStorage.setItem('bts.language', 'en'));
    const profile = 'custom:' + 'e'.repeat(64);
    let selected = false; let prepared = false; let prepareCalls = 0;
    let selection: Record<string, unknown> | null = null;
    let savedModel: Record<string, unknown> = {};
    await page.route('**/api/models', async route => {
      const response = await route.fetch(); const value = await response.json();
      savedModel = { ...value.ocr, engine: selected ? profile : 'tesseract', profile_names: { [profile]: 'custom/public-ocr · v1' }, profile_paths: { [profile]: ['/synthetic/ocr-adapter'] }, profile_devices: { [profile]: ['cpu'] } };
      await route.fulfill({ json: { ...value, ocr: savedModel } });
    });
    await page.route('**/api/media-index', async route => {
      const response = await route.fetch(); const value = await response.json();
      await route.fulfill({ json: { ...value, ocr_engine: selected ? profile : 'tesseract', ocr_runtime_installed: true, ocr_available: prepared } });
    });
    await page.route('**/api/models/ocr/device', async route => {
      selection = route.request().postDataJSON(); selected = true;
      await route.fulfill({ json: { model: { ...savedModel, engine: profile } } });
    });
    await page.route('**/api/media-index/prepare', async route => {
      prepareCalls++; prepared = true;
      expect(route.request().postDataJSON()).toMatchObject({ kind: 'ocr' });
      await route.fulfill({ status: 202, json: { ocr_engine: profile, ocr_runtime_installed: true, ocr_available: true, preparation_state: 'ready' } });
    });
    await page.goto('/');
    await page.getByRole('button', { name: english ? 'Settings' : 'Настройки', exact: true }).click();
    await page.getByRole('tab', { name: english ? 'Models' : 'Модели', exact: true }).click();
    const chooser = page.getByRole('combobox', { name: english ? 'OCR model' : 'Модель OCR', exact: true });
    await expect(chooser.locator(`option[value="${profile}"]`)).toHaveText('custom/public-ocr · v1');
    await chooser.selectOption(profile);
    const prepare = page.getByRole('button', { name: english ? 'Prepare OCR' : 'Подготовить OCR', exact: true });
    await expect(prepare).toBeDisabled();
    await page.getByRole('button', { name: english ? 'Save OCR selection' : 'Сохранить выбор OCR', exact: true }).click();
    await expect.poll(() => selection).toEqual({ device: 'cpu', ocr_engine: profile, defer_preparation: true });
    await expect(prepare).toBeEnabled(); expect(prepareCalls).toBe(0);
    await prepare.click();
    await expect.poll(() => prepareCalls).toBe(1);
  });
}

test('registration refresh recovers model settings and ignores late initial failure', async ({ page }) => {
  const identity = 'f'.repeat(64);
  const summary = { id: 'public-ocr', role: 'ocr', identity, runtime: { name: 'python', version: '3' } };
  let reads = 0;
  let release!: () => void;
  const delayed = new Promise<void>(resolve => { release = resolve; });
  await page.route('**/api/models', async route => {
    if (++reads === 1) {
      await delayed;
      return route.fulfill({ status: 503, json: { detail: 'Old model settings failure' } });
    }
    const response = await route.fetch(); const value = await response.json();
    return route.fulfill({ json: { ...value, ocr: { ...value.ocr, profile_names: { ['custom:' + identity]: 'custom/recovered-ocr' } } } });
  });
  await page.route('**/api/adapters', route => route.fulfill({ json: { models: [summary], errors: [], job: { id: 'g'.repeat(32), state: 'passed', action: 'validate', role: 'ocr', report: { ...summary, registered: true }, progress: null, error: null } } }));
  try {
    await page.goto('/');
    await page.getByRole('button', { name: 'Настройки', exact: true }).click();
    await page.getByRole('tab', { name: 'Модели', exact: true }).click();
    const chooser = page.getByRole('combobox', { name: 'Модель OCR', exact: true });
    await expect(chooser.locator(`option[value="custom:${identity}"]`)).toHaveText('custom/recovered-ocr');
    await expect(chooser).toBeEnabled();
    release();
    await expect(page.getByText('Old model settings failure', { exact: true })).toHaveCount(0);
    await expect(chooser).toBeEnabled();
  } finally { release(); }
});

for (const { devices, device } of [
  { devices: ['cpu', 'gpu'], device: 'gpu' }, { devices: ['gpu'], device: 'gpu' },
  { devices: ['cpu'], device: 'cpu' }, { devices: ['cpu'], device: 'auto' },
]) {
  test(`custom OCR performance choices follow declared devices (${devices.join('+')}/${device})`, async ({ page }) => {
    const profile = 'custom:' + '1'.repeat(64);
    const models = await (await page.request.get('/api/models')).json();
    models.ocr = { ...models.ocr, engine: profile, device,
      profile_devices: { [profile]: devices }, profile_names: { [profile]: 'custom/ocr-devices' } };
    await page.route('**/api/models', route => route.fulfill({ json: models }));
    await page.goto('/');
    await page.getByRole('button', { name: 'Настройки', exact: true }).click();
    const chooser = page.getByRole('region', { name: 'Устройства OCR', exact: true })
      .getByLabel('Устройство распознавания', { exact: true });
    await expect(chooser).toHaveValue(models.ocr.device);
    await expect.poll(() => chooser.locator('option').evaluateAll(options => options.map(option => (option as HTMLOptionElement).value))).toEqual([
      ...(devices.includes('cpu') ? ['cpu'] : []),
      'auto', ...(devices.includes('gpu') ? ['gpu'] : []),
      ...(devices.length === 2 ? ['hybrid'] : []),
    ]);
    await expect(chooser.locator(`option[value="${device}"]`)).toBeEnabled();
  });
}

test('late registration refresh cannot overwrite a newly saved OCR choice', async ({ page }) => {
  const identity = '2'.repeat(64);
  const profile = 'custom:' + identity;
  const summary = { id: 'public-ocr-race', role: 'ocr', identity, runtime: { name: 'python', version: '3' } };
  const models = await (await page.request.get('/api/models')).json();
  models.ocr = { ...models.ocr, engine: 'tesseract', device: 'cpu',
    profile_names: { [profile]: 'custom/ocr-race' }, profile_devices: { [profile]: ['cpu'] } };
  let reads = 0; let selected = false;
  let release!: () => void;
  const delayed = new Promise<void>(resolve => { release = resolve; });
  await page.route('**/api/models', async route => {
    const snapshot = structuredClone(models);
    if (++reads === 2) await delayed;
    return route.fulfill({ json: snapshot });
  });
  await page.route('**/api/adapters', route => route.fulfill({ json: { models: [summary], errors: [], job: {
    id: 'h'.repeat(32), state: 'passed', action: 'validate', role: 'ocr',
    report: { ...summary, registered: true }, progress: null, error: null,
  } } }));
  await page.route('**/api/models/ocr/device', route => {
    models.ocr = { ...models.ocr, engine: profile }; selected = true;
    return route.fulfill({ json: { model: models.ocr } });
  });
  await page.route('**/api/media-index', async route => {
    const base = await (await route.fetch()).json();
    return route.fulfill({ json: { ...base, ocr_engine: selected ? profile : 'tesseract',
      ocr_runtime_installed: true, ocr_available: false } });
  });
  try {
    await page.goto('/');
    await page.getByRole('button', { name: 'Настройки', exact: true }).click();
    await page.getByRole('tab', { name: 'Модели', exact: true }).click();
    await expect.poll(() => reads).toBe(2);
    const chooser = page.getByRole('combobox', { name: 'Модель OCR', exact: true });
    await chooser.selectOption(profile);
    await page.getByRole('button', { name: 'Сохранить выбор OCR', exact: true }).click();
    const prepare = page.getByRole('button', { name: 'Подготовить OCR', exact: true });
    await expect(prepare).toBeEnabled();
    release();
    await expect.poll(() => reads).toBeGreaterThan(2);
    await expect(chooser).toHaveValue(profile);
    await expect(prepare).toBeEnabled();
  } finally { release(); }
});
