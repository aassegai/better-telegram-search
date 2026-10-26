import { expect, test } from '@playwright/test';

test('an external top-level entry loads the app and remains usable after reload', async ({ page }) => {
  // A different hostname supplies real cross-site navigation metadata without
  // contacting any external service or using the user's application workspace.
  await page.route('http://localhost:8766/entry', route => route.fulfill({
    contentType: 'text/html', body: '<a href="http://127.0.0.1:8766/">Open app</a>',
  }));
  await page.goto('http://localhost:8766/entry');
  const navigation = page.waitForResponse(response => response.request().isNavigationRequest()
    && response.url() === 'http://127.0.0.1:8766/');
  await page.getByRole('link', { name: 'Open app' }).click();
  expect((await navigation).status()).toBe(200);
  await expect(page.getByLabel('Поисковый запрос')).toBeVisible();
  expect(await page.evaluate(async () => (await fetch('/api/session')).status)).toBe(200);
  expect((await page.reload())?.status()).toBe(200);
  await expect(page.getByLabel('Поисковый запрос')).toBeVisible();
  await page.getByRole('button', { name: 'Настройки', exact: true }).click();
  await expect(page.getByRole('dialog', { name: 'Настройки', exact: true })).toBeVisible();
});

test('an external top-level link cannot open the session API', async ({ page }) => {
  await page.route('http://localhost:8766/entry', route => route.fulfill({
    contentType: 'text/html', body: '<a href="http://127.0.0.1:8766/api/session">Open session</a>',
  }));
  await page.goto('http://localhost:8766/entry');
  const navigation = page.waitForResponse(response => response.request().isNavigationRequest()
    && response.url() === 'http://127.0.0.1:8766/api/session');
  await page.getByRole('link', { name: 'Open session' }).click();
  const response = await navigation;
  expect(response.status()).toBe(403);
  expect(await response.json()).toEqual({ detail: 'Доступ разрешён только из локального приложения.' });
});
