import { useEffect, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import { api } from './api';
import { t, uiLocale } from './i18n';
import { useDialogOperation } from './useDialogOperation';
import TelegramBindingForm from './TelegramBindingForm';
import type { Chat } from './types';

export type TelegramConnection = {
  configured: boolean; runtime_installed: boolean; connected: boolean;
  configuration_editable?: boolean; reconnect?: boolean;
  state: string; error: string | null; account_user_id: number | null; retry_after: number;
};
export const telegramStates: Record<string, string> = {
  disconnected: 'Telegram отключён', awaiting_code: 'Ожидается код Telegram', awaiting_2fa: 'Ожидается пароль 2FA',
  connecting: 'Подключение к Telegram', catching_up: 'Получение пропущенных событий', live: 'Telegram подключён',
  paused: 'Синхронизация на паузе', waiting_rate_limit: 'Ожидание разрешения Telegram',
  degraded: 'Синхронизация прервана', auth_required: 'Требуется вход в Telegram',
  queued: 'В очереди', running: 'Получение сообщений', partial: 'Проверка истории продолжается',
  completed: 'Сообщения получены', failed: 'Ошибка', cancelled: 'Отменено',
};
type Flow = { flow_id?: string; state: string };

export default function TelegramPanel({ chats, onChanged }: { chats: Chat[]; onChanged: () => Promise<void> }) {
  const [status, setStatus] = useState<TelegramConnection | null>(null);
  const [flow, setFlow] = useState<Flow | null>(null);
  const flowRef = useRef<Flow | null>(null);
  const [phone, setPhone] = useState('');
  const [code, setCode] = useState('');
  const [password, setPassword] = useState('');
  const [apiId, setApiId] = useState('');
  const [apiHash, setApiHash] = useState('');
  const [notice, setNotice] = useState('');
  const op = useDialogOperation();
  const busy = useRef(false);
  busy.current = op.busy;
  const updateFlow = (value: Flow | null) => {
    flowRef.current = value; setFlow(value);
    if (!value) { setCode(''); setPassword(''); }
  };
  useEffect(() => {
    let alive = true, pending = false;
    const poll = async () => {
      if (pending || busy.current) return;
      pending = true;
      const current = op.guard();
      try {
        const value = await api<TelegramConnection>('/api/telegram/connection');
        if (alive && current() && !busy.current) {
          setStatus(value);
          if (flowRef.current && !['awaiting_code', 'awaiting_2fa'].includes(value.state)) updateFlow(null);
        }
      } catch (error) { if (alive && current()) op.setError(error instanceof Error ? error.message : t('Ошибка соединения.')); }
      finally { pending = false; }
    };
    void poll();
    const timer = window.setInterval(() => void poll(), 2000);
    return () => {
      alive = false; window.clearInterval(timer);
      const id = flowRef.current?.flow_id;
      if (id) void api(`/api/telegram/auth/${encodeURIComponent(id)}`, { method: 'DELETE' }).catch(() => {});
      flowRef.current = null;
    };
  }, []);
  async function login(event: FormEvent) {
    event.preventDefault();
    const currentFlow = flow;
    const payload = currentFlow?.state === 'awaiting_2fa' ? { password } : currentFlow?.flow_id ? { code } : { phone };
    // Never retain login codes/passwords after a submission, including failed requests.
    setCode(''); setPassword(''); setNotice('');
    await op.run(async current => {
      const path = currentFlow?.flow_id ? `/api/telegram/auth/${encodeURIComponent(currentFlow.flow_id)}/${currentFlow.state === 'awaiting_2fa' ? 'password' : 'code'}` : '/api/telegram/auth/start';
      let result: Flow;
      try {
        result = await api<Flow>(path, { method: 'POST', body: JSON.stringify(payload) });
      } catch (error) {
        // Expired codes end the server flow. Return directly to phone entry;
        // invalid codes/passwords retain their retryable flow.
        try {
          const value = await api<TelegramConnection>('/api/telegram/connection');
          if (current()) {
            setStatus(value);
            if (!['awaiting_code', 'awaiting_2fa'].includes(value.state)) updateFlow(null);
          }
        } catch { /* Keep the original, sanitized login error. */ }
        throw error;
      }
      if (!current()) {
        if (result.flow_id) await api(`/api/telegram/auth/${encodeURIComponent(result.flow_id)}`, { method: 'DELETE' });
        return;
      }
      updateFlow(result.flow_id ? result : null);
      if (result.state === 'live') setPhone('');
      setStatus(await api<TelegramConnection>('/api/telegram/connection'));
    });
  }
  async function configure(event: FormEvent) {
    event.preventDefault();
    const credentials = { api_id: Number(apiId), api_hash: apiHash };
    setApiHash(''); setNotice('');
    await op.run(async current => {
      const value = await api<TelegramConnection>('/api/telegram/configuration', {
        method: 'PUT', body: JSON.stringify(credentials),
      });
      if (current()) {
        setStatus(value); setApiId('');
        setNotice(t('Настройка Telegram сохранена. Теперь войдите по номеру телефона.'));
      }
    });
  }
  const action = (name: string) => void op.run(async current => {
    setCode(''); setPassword(''); setPhone('');
    const value = await api<TelegramConnection & { server_revocation_confirmed?: boolean }>(`/api/telegram/${name}`, { method: 'POST' });
    if (current()) {
      updateFlow(null); setStatus(value);
      setNotice(name === 'logout' ? value.server_revocation_confirmed ? t('Авторизация Telegram отозвана. Архив сохранён.') : t('Локальная сессия удалена. Серверный выход не подтверждён: завершите эту сессию в Telegram → Настройки → Устройства.') : '');
    }
  });
  const cancel = () => void op.run(async current => {
    if (flow?.flow_id) await api(`/api/telegram/auth/${encodeURIComponent(flow.flow_id)}`, { method: 'DELETE' });
    if (current()) { updateFlow(null); setCode(''); setPassword(''); setStatus(await api('/api/telegram/connection')); }
  });
  return <section className="telegram-panel">
    <h3>{t('Подключение Telegram')}</h3>
    <p>{t('Приложение получает новые сообщения и фотографии только выбранных диалогов. Поиск работает и без подключения.')}</p>
    <p className="baseline-note">{t('Сессия даёт доступ к аккаунту Telegram и хранится локально без шифрования. Приложение не отправляет и не изменяет сообщения. Синхронизация работает, пока приложение запущено.')}</p>
    {!status ? <p>{t('Проверяем…')}</p> : <>
      <p role="status">{t(telegramStates[status.state] || status.state)}{status.account_user_id && <small> · ID {status.account_user_id}</small>}</p>
      {status.error && <p className="error" role="alert">{t(status.error)}</p>}
      {status.retry_after > Date.now() / 1000 && <p>{t('Следующая попытка: {p0}', { p0: new Date(status.retry_after * 1000).toLocaleTimeString(uiLocale()) })}</p>}
      {!status.configured && <p>{t('Добавьте API ID и API Hash приложения Telegram перед входом.')}</p>}
      {!status.runtime_installed && <p>{t('Для подключения Telegram установите дополнительный модуль telegram.')}</p>}
      {!status.connected && !flow && status.configuration_editable !== false && <details className="telegram-configuration" open={!status.configured}>
        <summary>{t('Настройка подключения Telegram')}</summary>
        <p className="baseline-note">{t('Укажите реквизиты своего приложения из API Development tools. Они сохранятся только на этом компьютере.')}</p>
        <p><a href="https://my.telegram.org/apps" target="_blank" rel="noopener noreferrer">{t('Открыть API Development tools')}</a></p>
        <form className="telegram-form" onSubmit={event => void configure(event)} autoComplete="off">
          <label>API ID<input type="number" min="1" max="2147483647" step="1" required value={apiId} disabled={op.busy} onChange={event => setApiId(event.target.value)} /></label>
          <label>API Hash<input type="password" autoComplete="off" minLength={32} maxLength={32} pattern="[a-fA-F0-9]{32}" required value={apiHash} disabled={op.busy} onChange={event => setApiHash(event.target.value)} /></label>
          <div className="dialog-actions"><button className="primary" disabled={op.busy}>{op.busy ? t('Сохраняем…') : t('Сохранить настройку Telegram')}</button></div>
        </form>
      </details>}
      {!status.configured && status.configuration_editable === false && <p>{t('Реквизиты Telegram заданы окружением. Измените их перед запуском.')}</p>}
      {!status.connected && status.configured && status.runtime_installed && <form className="telegram-form" onSubmit={event => void login(event)} autoComplete="off">
        {flow?.state === 'awaiting_2fa' ? <label>{t('Пароль двухэтапной проверки')}<input type="password" autoComplete="off" maxLength={256} value={password} onChange={event => setPassword(event.target.value)} disabled={op.busy} required /></label>
          : flow?.flow_id ? <label>{t('Код входа из Telegram')}<input autoComplete="off" inputMode="numeric" maxLength={16} value={code} onChange={event => setCode(event.target.value)} disabled={op.busy} required /></label>
            : <label>{t('Номер телефона с кодом страны')}<input type="tel" autoComplete="off" placeholder="+…" maxLength={16} value={phone} onChange={event => setPhone(event.target.value)} disabled={op.busy} required /></label>}
        <div className="dialog-actions"><button className="primary" disabled={op.busy || status.retry_after > Date.now() / 1000}>{op.busy ? t('Подключаем…') : flow?.flow_id ? t('Подтвердить вход') : t('Получить код')}</button>
          {flow?.flow_id && <button type="button" disabled={op.busy} onClick={cancel}>{t('Отменить вход')}</button>}
          {!flow && status.account_user_id && <button type="button" disabled={op.busy} onClick={() => action('connect')}>{t('Подключить сохранённую сессию')}</button>}</div>
      </form>}
      {(status.connected || status.reconnect || status.account_user_id) && !flow && <div className="dialog-actions">
        {(status.connected || status.reconnect) && <button disabled={op.busy} onClick={() => action('disconnect')}>{t('Отключить Telegram')}</button>}
        <button disabled={op.busy} onClick={() => action('logout')}>{t('Выйти из Telegram')}</button></div>}
      {status.connected && <TelegramBindingForm chats={chats} onChanged={onChanged} />}
    </>}
    {notice && <p role="status">{t(notice)}</p>}{op.error && <p className="error" role="alert">{t(op.error)}</p>}
  </section>;
}
