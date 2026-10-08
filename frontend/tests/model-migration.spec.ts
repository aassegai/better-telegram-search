import { expect, test } from '@playwright/test';

for (const english of [false, true]) {
  test(`new model switches require explicit reindex; Giga is a separate role (${english ? 'EN' : 'RU'})`, async ({ page }) => {
    if (english) await page.addInitScript(() => localStorage.setItem('bts.language', 'en'));
    await page.route('**/api/semantic', async route => {
      const response = await route.fetch(); const status = await response.json();
      await route.fulfill({ json: { ...status, runtime_installed: true, enabled: 1, profile: 'small', preparation_state: 'ready' } });
    });
    await page.route('**/api/media-index', async route => {
      const response = await route.fetch(); const status = await response.json();
      await route.fulfill({ json: { ...status, images_enabled: 1, visual_profile: 'clip' } });
    });
    let giga = { enabled: false, preparation_state: 'idle', error: null, path: '/synthetic/models/giga',
      device: 'cpu', download_bytes: 968510245, download_completed_bytes: 0 };
    await page.route('**/api/rerank', route => route.fulfill({ json: giga }));
    await page.route('**/api/rerank/prepare', route => {
      expect(route.request().postDataJSON()).toEqual({ offline: false });
      giga = { ...giga, preparation_state: 'ready' }; return route.fulfill({ json: giga });
    });
    await page.route('**/api/settings', route => {
      if (route.request().method() !== 'PATCH') return route.continue();
      expect(route.request().postDataJSON()).toEqual({ giga_rerank_enabled: true });
      giga = { ...giga, enabled: true }; return route.fulfill({ json: {} });
    });
    await page.goto('/');
    await page.getByRole('button', { name: english ? 'Settings' : 'Настройки', exact: true }).click();
    const model = page.getByLabel(english ? 'Semantic search model' : 'Модель смыслового поиска', { exact: true });
    await model.selectOption('berta');
    const prepareText = page.getByRole('button', { name: english ? 'Prepare text model' : 'Подготовить модель текста', exact: true });
    await expect(prepareText).toBeDisabled();
    await page.getByLabel(english ? 'Allow reindexing all chats when changing the model' : 'Разрешить переиндексацию всех диалогов при смене модели').check();
    await expect(prepareText).toBeEnabled();
    const visual = page.getByLabel(english ? 'Visual model' : 'Визуальная модель', { exact: true });
    await visual.selectOption('siglip2');
    const prepareImages = page.getByRole('button', { name: english ? 'Prepare image model' : 'Подготовить модель изображений', exact: true });
    await expect(prepareImages).toBeDisabled();
    await page.getByLabel(english ? 'Rebuild the image index when switching models; keep the OCR cache' : 'Перестроить индекс изображений при смене модели; кэш OCR сохранится').check();
    await expect(prepareImages).toBeEnabled();
    const rerank = page.getByLabel(english ? 'Giga reranking' : 'Переранжирование Giga', { exact: true });
    await expect(rerank).not.toBeChecked(); await expect(rerank).toBeDisabled();
    await page.getByRole('button', { name: english ? 'Prepare Giga' : 'Подготовить Giga', exact: true }).click();
    await expect(rerank).toBeEnabled(); await rerank.check();
    await expect(rerank).toBeChecked();
    await page.keyboard.press('Escape');
    await page.getByRole('button', { name: english ? 'Settings' : 'Настройки', exact: true }).click();
    await expect(rerank).toBeChecked();
  });
}
