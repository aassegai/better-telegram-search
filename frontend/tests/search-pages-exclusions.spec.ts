import { expect, test } from '@playwright/test';

const hit = (id: number) => ({ chat_id: 'synthetic', chat_name: 'Тест', message_id: id,
  timestamp: 1750000000, matched_by: ['words'], messages: [{ chat_id: 'synthetic', message_id: id,
    timestamp: 1750000000, author: 'Автор', text: `проверка ${id}`, kind: 'message',
    media: [], matches_filters: true }] });

for (const english of [false, true]) {
  test(`cached paging keeps order and uses submitted search (${english ? 'EN' : 'RU'})`, async ({ page }) => {
    let searches = 0;
    let pages = 0;
    await page.route('**/api/search?**', route => {
      searches++;
      return route.fulfill({ json: { results: [hit(1), hit(2)], has_more: true, effective_mode: 'words',
        warnings: [], limit: 2, search_id: 'snapshot', next_offset: 2, cached_results: 4 } });
    });
    await page.route('**/api/search/snapshot/page?**', async route => {
      pages++;
      expect(new URL(route.request().url()).searchParams.get('offset')).toBe('2');
      await route.fulfill({ json: { results: [hit(3), hit(4)], has_more: false, next_offset: null } });
    });
    await page.goto('/');
    if (english) await page.getByRole('slider').press('End');
    await page.getByLabel(english ? 'Search query' : 'Поисковый запрос').fill('проверка');
    await page.getByRole('button', { name: english ? 'Search' : 'Найти', exact: true }).click();
    await expect(page.locator('.result-card')).toHaveCount(2);
    await page.getByLabel(english ? 'Search query' : 'Поисковый запрос').fill('draft query');
    await page.getByRole('button', { name: english ? 'Show more' : 'Показать ещё', exact: true }).click();
    await expect(page.locator('.result-card')).toHaveCount(4);
    await expect(page.locator('.result-card .message-text')).toHaveText(['проверка 1', 'проверка 2', 'проверка 3', 'проверка 4']);
    await expect(page.getByRole('button', { name: english ? 'Show more' : 'Показать ещё', exact: true })).toHaveCount(0);
    expect([searches, pages]).toEqual([1, 1]);
  });
}

test('late cached page cannot contaminate a new search', async ({ page }) => {
  let release: () => void = () => {};
  let requested = false;
  await page.route('**/api/search?**', route => {
    const first = new URL(route.request().url()).searchParams.get('q') === 'first';
    return route.fulfill({ json: { results: [hit(first ? 1 : 99)], has_more: first,
      effective_mode: 'words', warnings: [], limit: 1, search_id: 'snapshot', next_offset: first ? 1 : null } });
  });
  await page.route('**/api/search/snapshot/page?**', async route => {
    requested = true;
    await new Promise<void>(resolve => { release = resolve; });
    await route.fulfill({ json: { results: [hit(2)], has_more: false, next_offset: null } });
  });
  await page.goto('/');
  await page.getByLabel('Поисковый запрос').fill('first');
  await page.getByRole('button', { name: 'Найти', exact: true }).click();
  await page.getByRole('button', { name: 'Показать ещё', exact: true }).click();
  await expect.poll(() => requested).toBe(true);
  await page.getByLabel('Поисковый запрос').fill('second');
  await page.getByRole('button', { name: 'Найти', exact: true }).click();
  await expect(page.locator('.message-text')).toHaveText(['проверка 99']);
  release();
  await expect(page.locator('.message-text')).toHaveText(['проверка 99']);
});

test('author exclusions survive polling, failure, saving and reopening', async ({ page }) => {
  await page.goto('/');
  const token = (await (await page.request.get('/api/session')).json()).token;
  const chat = (await (await page.request.get('/api/chats')).json())[0];
  const settings = `/api/chats/${chat.id}/index/settings`;
  let fail = true;
  await page.route(`**${settings}`, route => fail
    ? route.fulfill({ status: 500, json: { detail: 'Синтетическая ошибка сохранения' } })
    : route.continue());
  try {
    await page.getByRole('button', { name: /^Настройки индексации / }).first().click();
    const panel = page.getByRole('region', { name: 'Исключения авторов' });
    await panel.locator('summary').click();
    const choices = panel.locator('input[type=checkbox]');
    await expect(choices.first()).toBeEnabled();
    await choices.first().check();
    const selectedLabel = await choices.first().locator('..').innerText();
    await expect(panel.getByRole('button', { name: 'Сохранить исключения', exact: true })).toBeEnabled();
    await page.waitForResponse(response => response.url().endsWith(`/api/chats/${chat.id}/index`) && response.ok());
    await expect(choices.first()).toBeChecked();
    await panel.getByRole('button', { name: 'Сохранить исключения', exact: true }).click();
    await expect(panel.getByRole('alert')).toContainText('Синтетическая ошибка сохранения');
    await expect(choices.first()).toBeChecked();
    fail = false;
    await panel.getByRole('button', { name: 'Сохранить исключения', exact: true }).click();
    await expect(panel.getByRole('button', { name: 'Сохранить исключения', exact: true })).toBeDisabled();
    await page.keyboard.press('Escape');
    await page.getByRole('button', { name: /^Настройки индексации / }).first().click();
    await panel.locator('summary').click();
    await expect(choices.first()).toBeChecked();
    expect(await choices.first().locator('..').innerText()).toBe(selectedLabel);
    await page.getByRole('slider').press('End');
    await expect(page.getByRole('region', { name: 'Author exclusions' })).toContainText('Exclude authors from indexing');
  } finally {
    await page.request.patch(settings, { headers: { 'X-Session-Token': token }, data: { excluded_author_ids: [] } });
  }
});
