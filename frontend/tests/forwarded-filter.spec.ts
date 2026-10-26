import { mockSearchJobs } from './search-mocks';
import { expect, test } from '@playwright/test';

test('forward authors have searchable checkboxes, combined selection and a reset', async ({ page }) => {
  const names = ['Альфа', 'Бета <img src=x onerror=window.forwardInjected=true>'];
  const queries: string[] = [];
  const searches: { forwarded_from: string[]; author_ids: string[] }[] = [];
  let finish!: () => void;
  await page.route('**/api/forwarded-sources?**', route => {
    const q = new URL(route.request().url()).searchParams.get('q') ?? '';
    queries.push(q);
    return route.fulfill({ json: { sources: names.filter(name => name.toLocaleLowerCase().includes(q.toLocaleLowerCase()))
      .map(name => ({ name, messages: 5 })), next_offset: null } });
  });
  await mockSearchJobs(page, async route => {
    searches.push(route.request().postDataJSON());
    await new Promise<void>(resolve => { finish = resolve; });
    await route.fulfill({ json: { results: [], effective_mode: 'words', warnings: [], has_more: false } });
  });
  await page.goto('/');
  expect(queries).toHaveLength(0);
  const filter = page.locator('.forward-filter');
  await filter.locator('summary').click();
  const input = filter.getByRole('searchbox', { name: 'Найти автора пересылки' });
  await input.fill('альфа');
  await filter.getByRole('checkbox', { name: /^Альфа/ }).check();
  await input.fill('бета');
  await filter.getByRole('checkbox', { name: /^Бета/ }).check();
  await expect(filter.getByRole('checkbox', { name: /^Альфа/ })).toBeChecked();
  await expect(filter.locator('summary')).toContainText('Выбрано источников: 2');
  expect(await page.evaluate(() => (window as unknown as Record<string, unknown>).forwardInjected)).toBeUndefined();
  await expect(filter.locator('img')).toHaveCount(0);
  await page.getByLabel('Автор', { exact: true }).selectOption('bob');
  await page.getByLabel('Поисковый запрос').fill('велосипед');
  await input.press('Enter');
  expect(searches).toHaveLength(0);
  await page.getByRole('button', { name: 'Найти', exact: true }).click();
  await expect.poll(() => searches.length).toBe(1);
  expect(searches[0].forwarded_from).toEqual(names);
  expect(searches[0].author_ids).toEqual(['bob']);
  await expect(filter.getByRole('checkbox', { name: /^Альфа/ })).toBeDisabled();
  finish();
  await expect(page.getByRole('button', { name: 'Найти', exact: true })).toBeEnabled();
  await page.getByRole('button', { name: 'Сбросить фильтры', exact: true }).click();
  await expect(filter.locator('summary')).toContainText('Все источники');
  await page.getByRole('button', { name: 'Найти', exact: true }).click();
  await expect.poll(() => searches.length).toBe(2);
  expect(searches[1].forwarded_from).toEqual([]);
  finish();
  await expect(page.getByRole('button', { name: 'Найти', exact: true })).toBeEnabled();
});

test('failed source pages preserve selection, retry and fit a narrow window', async ({ page }) => {
  let failed = true;
  await page.route('**/api/forwarded-sources?**', route => {
    const offset = Number(new URL(route.request().url()).searchParams.get('offset'));
    if (offset && failed) return route.fulfill({ status: 500, json: { detail: 'Synthetic error <img src=x onerror=window.sourceError=true>' } });
    return route.fulfill({ json: { sources: [{ name: offset ? 'Next author' : 'First author', messages: 1 }], next_offset: offset ? null : 1 } });
  });
  await page.setViewportSize({ width: 320, height: 640 });
  await page.goto('/');
  const filter = page.locator('.forward-filter');
  await filter.locator('summary').click();
  await filter.getByRole('checkbox', { name: /^First author/ }).check();
  await filter.getByRole('button', { name: 'Показать ещё источники' }).click();
  await expect(filter.getByRole('alert')).toContainText('Synthetic error <img');
  await expect(filter.getByRole('checkbox', { name: /^First author/ })).toBeChecked();
  await expect(filter.getByRole('button', { name: 'Показать ещё источники' })).toBeDisabled();
  expect(await filter.evaluate(node => node.scrollWidth <= node.clientWidth + 1)).toBe(true);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
  await page.getByRole('button', { name: 'RU', exact: true }).press('End');
  await expect(filter.getByRole('button', { name: 'Retry', exact: true })).toBeVisible();
  expect(await filter.evaluate(node => node.scrollWidth <= node.clientWidth + 1)).toBe(true);
  expect(await page.evaluate(() => (window as unknown as Record<string, unknown>).sourceError)).toBeUndefined();
  failed = false;
  await filter.getByRole('button', { name: 'Retry', exact: true }).click();
  await expect(filter.getByRole('checkbox', { name: /^Next author/ })).toBeVisible();
  await expect(filter.getByRole('checkbox', { name: /^First author/ })).toBeChecked();
  await expect(filter.getByRole('alert')).toHaveCount(0);
});

test('source pagination restarts when a query returns and stale responses are ignored', async ({ page }) => {
  let releaseOld!: () => void;
  const offsets: number[] = [];
  await page.route('**/api/forwarded-sources?**', async route => {
    const params = new URL(route.request().url()).searchParams;
    const q = params.get('q') ?? '';
    const offset = Number(params.get('offset'));
    if (q === 'old') {
      await new Promise<void>(resolve => { releaseOld = resolve; });
      return route.fulfill({ json: { sources: [{ name: 'Stale source', messages: 1 }], next_offset: null } });
    }
    if (q === '') offsets.push(offset);
    return route.fulfill({ json: { sources: [{ name: q || (offset ? 'Second page' : 'First page'), messages: 1 }], next_offset: q === '' && offset === 0 ? 1 : null } });
  });
  await page.goto('/');
  const filter = page.locator('.forward-filter');
  await filter.locator('summary').click();
  await expect(filter.getByRole('checkbox', { name: /^First page/ })).toBeVisible();
  await filter.getByRole('button', { name: 'Показать ещё источники' }).click();
  await expect(filter.getByRole('checkbox')).toHaveCount(2);
  const input = filter.getByRole('searchbox');
  await input.fill('old');
  await expect.poll(() => !!releaseOld).toBe(true);
  await input.fill('current');
  await expect(filter.getByRole('checkbox', { name: /^current/ })).toBeVisible();
  const oldResponse = page.waitForResponse(response => new URL(response.url()).searchParams.get('q') === 'old');
  releaseOld();
  await oldResponse;
  await expect(filter.getByRole('checkbox', { name: /^Stale/ })).toHaveCount(0);
  await input.fill('');
  await expect(filter.getByRole('checkbox', { name: /^First page/ })).toBeVisible();
  expect(offsets).toEqual([0, 1, 0]);
  await page.setViewportSize({ width: 320, height: 640 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
  await page.getByRole('button', { name: 'RU', exact: true }).press('End');
  await expect(input).toHaveAccessibleName('Find a forward author');
  await expect(filter.locator('summary')).toContainText('Forwarded from');
});
