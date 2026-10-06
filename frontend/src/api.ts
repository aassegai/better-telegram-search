import { t } from './i18n';
let token = '';
export async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(path, {
    ...options,
    headers: { 'Content-Type': 'application/json', 'X-Session-Token': token, ...options.headers },
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(typeof error.detail === 'string' ? error.detail : t('Не удалось выполнить запрос.'));
  }
  return response.json() as Promise<T>;
}
export async function initializeSession() {
  const result = await api<{ token: string }>('/api/session');
  token = result.token;
}
