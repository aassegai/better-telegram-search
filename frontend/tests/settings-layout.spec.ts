import { expect, test } from '@playwright/test';

for (const variant of [
  { language: 'ru', theme: 'light', width: 1280, height: 800, zoom: 1 },
  { language: 'en', theme: 'dark', width: 1280, height: 800, zoom: 1.25 },
  { language: 'ru', theme: 'dark', width: 390, height: 844, zoom: 1 },
  { language: 'en', theme: 'light', width: 390, height: 844, zoom: 1.5 },
] as const) {
  test.describe(`${variant.language}/${variant.theme}/${variant.width}/${variant.zoom}`, () => {
    // Browser zoom reduces the CSS viewport and scales the rendered CSS pixels.
    test.use({ viewport: { width: Math.round(variant.width / variant.zoom), height: Math.round(variant.height / variant.zoom) }, deviceScaleFactor: variant.zoom });
    test('settings controls stay reachable', async ({ page }) => {
    await page.addInitScript(({ language, theme }) => {
      localStorage.setItem('bts.language', language);
      localStorage.setItem('bts.theme', theme);
    }, variant);
    await page.goto('/');
    const english = variant.language === 'en';
    await page.getByRole('button', { name: english ? 'Settings' : 'Настройки', exact: true }).click();
    const dialog = page.getByRole('dialog');
    const close = dialog.getByRole('button', { name: english ? 'Close' : 'Закрыть', exact: true });
    const panel = dialog.locator('.modal');
    for (const name of english ? ['Performance', 'Models', 'Database', 'Updates'] : ['Производительность', 'Модели', 'База', 'Обновления']) {
      await dialog.getByRole('tab', { name, exact: true }).click();
      await expect(dialog.getByRole('tab', { name, exact: true })).toHaveAttribute('aria-selected', 'true');
      expect(await panel.evaluate(element => element.scrollWidth <= element.clientWidth + 1)).toBe(true);
      await expect(close).toBeInViewport({ ratio: 1 });
    }
    await dialog.getByRole('tab', { name: english ? 'Models' : 'Модели', exact: true }).click();
    await expect(dialog.locator('.model-card')).toHaveCount(4);
    const initial = (await close.boundingBox())!;
    const bounds = (await panel.boundingBox())!;
    expect(initial.y + initial.height).toBeLessThanOrEqual(bounds.y);
    await panel.evaluate(element => { element.scrollTop = element.scrollHeight; });
    await expect(close).toBeInViewport({ ratio: 1 });
    expect((await close.boundingBox())!.y).toBeCloseTo(initial.y, 1);
    const icon = (await close.locator('svg').boundingBox())!;
    expect(icon.x + icon.width / 2).toBeCloseTo(initial.x + initial.width / 2, 1);
    expect(icon.y + icon.height / 2).toBeCloseTo(initial.y + initial.height / 2, 1);
    await close.focus();
    await page.keyboard.press('Tab');
    expect(await dialog.evaluate(element => element.contains(document.activeElement))).toBe(true);
    await page.screenshot({ path: `../workspace/analysis/settings-${variant.language}-${variant.theme}-${variant.width}-${variant.zoom}.png` });
    await close.click({ position: { x: 7, y: initial.height / 2 } });
    await expect(dialog).toHaveCount(0);
    });
  });
}
