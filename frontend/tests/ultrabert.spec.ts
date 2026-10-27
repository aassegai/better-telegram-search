import { test, expect } from './fixtures';

for (const english of [false, true]) {
  test(`UltraBERT requires explicit migration and language preserves draft (${english ? 'EN' : 'RU'})`, async ({ page }) => {
    if (english) await page.addInitScript(() => localStorage.setItem('bts.language', 'en'));
    let prepares = 0;
    await page.route('**/api/semantic', async route => {
      const response = await route.fetch(); const status = await response.json();
      await route.fulfill({ json: { ...status, runtime_installed: true, dense_available: true,
        enabled: 1, preparation_state: 'ready', profile: 'berta' } });
    });
    await page.route('**/api/semantic/prepare', async route => {
      prepares++;
      expect(route.request().postDataJSON()).toMatchObject({ profile: 'ultrabert', reindex: true });
      await route.fulfill({ status: 409, json: { detail: 'Synthetic preparation blocked' } });
    });
    await page.goto('/');
    await page.getByRole('button', { name: english ? 'Settings' : 'Настройки', exact: true }).click();
    await page.getByRole('tab', { name: english ? 'Models' : 'Модели', exact: true }).click();
    const model = page.getByLabel(english ? 'Semantic search model' : 'Модель смыслового поиска', { exact: true });
    await expect(model).toHaveValue('berta');
    await expect(model.locator('option[value=ultrabert]')).toHaveText('UltraBERT-Embed-150M');
    await model.selectOption('ultrabert');
    const migrate = page.getByRole('button', { name: english ? 'Switch model and rebuild text' : 'Сменить модель и перестроить текст', exact: true });
    await expect(migrate).toBeDisabled();
    expect(prepares).toBe(0);
    await page.getByLabel(english ? 'Allow reindexing all chats when changing the model' : 'Разрешить переиндексацию всех диалогов при смене модели').check();
    await migrate.click();
    await expect(page.getByText('Synthetic preparation blocked', { exact: true })).toBeVisible();
    expect(prepares).toBe(1);
    await expect(model).toHaveValue('ultrabert');
    await page.getByRole('button', { name: english ? 'RU' : 'EN', exact: true }).click();
    await expect(page.getByLabel(english ? 'Модель смыслового поиска' : 'Semantic search model', { exact: true })).toHaveValue('ultrabert');
    expect(prepares).toBe(1);
  });
}
