import { test as base, expect } from '@playwright/test';

/** Each test starts with a fresh tab workspace, while reloads within it persist. */
export const test = base.extend<{ cleanTabs: void }>({
  cleanTabs: [async ({ page }, use) => {
    const token = (await (await page.request.get('/api/session')).json()).token;
    const headers = { 'X-Session-Token': token };
    const response = await page.request.get('/api/search-tabs');
    expect(response.ok()).toBeTruthy();
    const { tabs } = await response.json();
    for (const tab of tabs) {
      const removed = await page.request.delete(`/api/search-tabs/${tab.id}`, { headers });
      expect(removed.status()).toBe(204);
    }
    await use();
  }, { auto: true }],
});
export { expect };
