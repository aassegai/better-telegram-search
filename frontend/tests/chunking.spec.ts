import { expect, test } from '@playwright/test';

for (const english of [false, true]) {
  test(`chunk policy is explicit and survives polling and failed saves (${english ? 'EN' : 'RU'})`, async ({ page }) => {
    await page.goto('/');
    const token = (await (await page.request.get('/api/session')).json()).token;
    const chat = (await (await page.request.get('/api/chats')).json())[0];
    const settings = `/api/chats/${chat.id}/index/settings`;
    let fail = true;
    await page.route(`**${settings}`, route => fail
      ? route.fulfill({ status: 500, json: { detail: 'Synthetic failure' } }) : route.continue());
    try {
      if (english) await page.getByRole('slider').press('End');
      await page.getByRole('button', { name: english ? /^Indexing settings / : /^Настройки индексации / }).first().click();
      const panel = page.locator('.chunking-settings');
      await panel.locator('summary').click();
      const select = panel.getByRole('combobox', { name: english ? 'Chunking rules' : 'Правила чанкинга' });
      const save = panel.getByRole('button', { name: english ? 'Apply and rebuild text index' : 'Применить и переиндексировать текст', exact: true });
      await expect(select).toHaveValue('legacy');
      await expect(save).toBeDisabled();
      await select.selectOption('episodes');
      await page.waitForResponse(response => response.url().endsWith(`/api/chats/${chat.id}/index`) && response.ok());
      await expect(select).toHaveValue('episodes');
      await save.click();
      await expect(panel.getByRole('alert')).toHaveText('Synthetic failure');
      await expect(select).toHaveValue('episodes');
      fail = false;
      await save.click();
      await expect(save).toBeDisabled();
      await page.keyboard.press('Escape');
      await page.getByRole('button', { name: english ? /^Indexing settings / : /^Настройки индексации / }).first().click();
      await panel.locator('summary').click();
      await expect(select).toHaveValue('episodes');
    } finally {
      await page.request.patch(settings, { headers: { 'X-Session-Token': token }, data: { chunking_profile: 'legacy' } });
    }
  });
}
