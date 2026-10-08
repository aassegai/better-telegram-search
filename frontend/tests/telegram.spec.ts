import { test, expect } from '@playwright/test';

const disconnected = { configured: true, runtime_installed: true, connected: false,
  state: 'disconnected', error: null, account_user_id: null, retry_after: 0 };

test('Telegram login clears secrets, cancels the flow on close, and translates notices', async ({ page }) => {
  let connection = disconnected;
  let cancelled = false;
  await page.route('**/api/telegram/connection', route => route.fulfill({ json: connection }));
  await page.route('**/api/telegram/auth/start', route => {
    expect(route.request().postDataJSON()).toEqual({ phone: '+10000000000' });
    connection = { ...disconnected, state: 'awaiting_code' };
    return route.fulfill({ json: { flow_id: 'synthetic-flow', state: 'awaiting_code' } });
  });
  await page.route('**/api/telegram/auth/synthetic-flow/code', route => {
    expect(route.request().postDataJSON()).toEqual({ code: '12345' });
    connection = { ...disconnected, state: 'awaiting_2fa' };
    return route.fulfill({ json: { flow_id: 'synthetic-flow', state: 'awaiting_2fa' } });
  });
  await page.route('**/api/telegram/auth/synthetic-flow', route => {
    expect(route.request().method()).toBe('DELETE');
    cancelled = true; connection = disconnected;
    return route.fulfill({ json: connection });
  });
  await page.goto('/');
  await page.getByRole('button', { name: 'Источники · Telegram' }).click();
  await page.getByLabel('Номер телефона с кодом страны').fill('+10000000000');
  await page.getByRole('button', { name: 'Получить код', exact: true }).click();
  await page.getByLabel('Код входа из Telegram').fill('12345');
  await page.getByRole('button', { name: 'Подтвердить вход' }).click();
  await page.getByLabel('Пароль двухэтапной проверки').fill('synthetic-private-password');
  const storage = await page.evaluate(() => JSON.stringify({ ...localStorage, ...sessionStorage }));
  expect(storage).not.toMatch(/12345|synthetic-private-password|10000000000/);
  await page.keyboard.press('Escape');
  await expect.poll(() => cancelled).toBe(true);
  await page.getByRole('button', { name: 'Источники · Telegram' }).click();
  await expect(page.getByLabel('Номер телефона с кодом страны')).toHaveValue('');
  await expect(page.locator('input[type=password]')).toHaveCount(0);
});

test('Telegram binding requires preview and account confirmation; success follows language', async ({ page }) => {
  const connected = { ...disconnected, connected: true, state: 'live', account_user_id: 999 };
  await page.route('**/api/telegram/connection', route => route.fulfill({ json: connected }));
  await page.route('**/api/telegram/dialogs?**', route => route.fulfill({ json: {
    dialogs: [{ key: 'user:100', peer_id: 100, peer_type: 'user', name: 'Synthetic remote' }], next_cursor: 'next-page',
  } }));
  await page.route('**/api/telegram/bindings/preview', route => {
    expect(route.request().postDataJSON()).toMatchObject({ peer_key: 'user:100', chat_id: null });
    return route.fulfill({ json: { preview_token: 'synthetic-preview-token-123456', revision: null,
      matched: 0, edited: 0, mismatches: 0, unavailable: 0, baseline_id: 1, can_bind: true, typed_id_match: false } });
  });
  await page.route('**/api/telegram/bindings', route => {
    expect(route.request().postDataJSON()).toMatchObject({ confirm_account: true, expected_revision: null });
    return route.fulfill({ json: { chat_id: 'synthetic' } });
  });
  await page.route('**/api/telegram/logout', route => route.fulfill({ json: {
    ...disconnected, server_revocation_confirmed: false,
  } }));
  await page.goto('/');
  await page.getByRole('button', { name: 'Источники · Telegram' }).click();
  await page.getByRole('button', { name: 'Показать диалоги Telegram' }).click();
  await page.getByLabel('Диалог Telegram', { exact: true }).selectOption('user:100');
  await page.getByRole('button', { name: 'Проверить привязку' }).click();
  const bind = page.getByRole('button', { name: 'Подключить диалог', exact: true });
  await expect(bind).toBeDisabled();
  await page.getByLabel('Подтверждаю, что это нужный аккаунт и диалог Telegram').check();
  await bind.click();
  await expect(page.getByText('Диалог подключён. Получение новых сообщений запущено.')).toBeVisible();
  await page.getByRole('slider').press('End');
  await expect(page.getByText('Chat connected. Fetching new messages has started.')).toBeVisible();
  await page.getByRole('button', { name: 'Log out of Telegram', exact: true }).click();
  await expect(page.getByRole('status').filter({ hasText: 'Server logout' })).toBeVisible();
});

test('chat sync shows the run state, errors and pause beside the connection state', async ({ page }) => {
  const state = { binding: { revision: 1, enabled: 1, download_media: 1,
    deletion_policy: 'archive', reconcile_days: 7, last_success_at: null },
    connection_state: 'live', run: { state: 'failed', added: 2, updated: 1, error: null },
    media: { ready: 3, pending: 4, failed: 5 }, cursor: { scanned_through_id: 99, baseline_id: 1, coverage: 'partial_export' } };
  await page.route('**/api/chats/*/telegram', route => route.fulfill({ json: state }));
  await page.route('**/api/chats/*/sync-settings', route => {
    expect(route.request().postDataJSON()).toEqual({ expected_revision: 1, enabled: false });
    return route.fulfill({ json: { ...state, binding: { ...state.binding, enabled: 0, revision: 2 } } });
  });
  await page.goto('/');
  await page.getByRole('button', { name: /^Настройки индексации /  }).first().click();
  const card = page.locator('.telegram-sync-card');
  await expect(card.getByRole('status')).toHaveText('Telegram подключён · Ошибка');
  await expect(card).toContainText('Не загружено фотографий: 5');
  await card.getByRole('button', { name: 'Пауза синхронизации' }).click();
  await expect(card.getByRole('status')).toHaveText('Синхронизация на паузе');
});

test('OCR evidence identifies fuzzy matches and archived deletion filter reaches search', async ({ page }) => {
  await page.route('**/api/search?**', route => {
    expect(new URL(route.request().url()).searchParams.get('exclude_deleted')).toBe('true');
    return route.fulfill({ json: { results: [{ chat_id: 'synthetic', message_id: 1, chat_name: 'Synthetic',
      messages: [], matched_by: ['ocr_words'], ocr_match: { kind: 'fuzzy', edits: 1 },
      ocr_text: 'synthetic', timestamp: 1750000000 }], warnings: [], has_more: false, effective_mode: 'words' } });
  });
  await page.goto('/');
  await page.getByLabel('Скрыть удалённые в Telegram').check();
  await page.getByLabel('Поисковый запрос').fill('synthetic');
  await page.getByRole('button', { name: 'Найти', exact: true }).click();
  await expect(page.getByText('OCR: неточное совпадение · отличий: 1')).toBeVisible();
});
