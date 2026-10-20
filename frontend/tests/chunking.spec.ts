import { expect, test } from '@playwright/test';

for (const english of [false, true]) {
  test(`advanced chunking is the default without a policy selector (${english ? 'EN' : 'RU'})`, async ({ page }) => {
    const writes: string[] = [];
    page.on('request', request => {
      if (request.method() === 'PATCH' && request.url().endsWith('/index/settings')) writes.push(request.postData() ?? '');
    });
    await page.goto('/');
    const chat = (await (await page.request.get('/api/chats')).json())[0];
    const before = await (await page.request.get(`/api/chats/${chat.id}/index`)).json();
    expect(before.chunking.profile).toBe('episodes');
    expect(before.chunking.max_messages).toBe(8);
    expect(before.chunking.max_tokens).toBe(480);
    if (english) await page.getByRole('slider').press('End');
    const open = page.getByRole('button', { name: english ? /^Indexing settings / : /^Настройки индексации / }).first();
    await open.click();
    const dialog = page.getByRole('dialog');
    await expect(dialog.locator('.index-card-heading h3').first()).toHaveText(english ? 'Text' : 'Текст');
    await expect(dialog.locator('.chunking-settings')).toHaveCount(0);
    await expect(dialog.getByRole('combobox', { name: english ? 'Chunking rules' : 'Правила чанкинга' })).toHaveCount(0);
    await page.waitForResponse(response => response.url().endsWith(`/api/chats/${chat.id}/index`) && response.ok());
    expect(writes).toEqual([]);
    await page.keyboard.press('Escape');
    await open.click();
    await expect(dialog.locator('.chunking-settings')).toHaveCount(0);
    const after = await (await page.request.get(`/api/chats/${chat.id}/index`)).json();
    expect(after.chunking).toEqual(before.chunking);
    expect(after.semantic.total_segments).toBe(before.semantic.total_segments);
  });
}
