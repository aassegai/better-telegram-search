import { useEffect, useRef, useState } from 'react';
import { api } from './api';
import { t, uiLocale } from './i18n';
import { telegramStates } from './TelegramPanel';
import { useDialogOperation } from './useDialogOperation';

type Binding = { revision: number; enabled: boolean | number; download_media: boolean | number; deletion_policy: 'archive' | 'mirror'; reconcile_days: number; last_success_at: number | null };
type Status = { binding: Binding | null; cursor?: { baseline_id: number; scanned_through_id: number; upper_bound: number | null; recent_upper_bound: number | null; coverage: string }; run?: { state: string; added: number; updated: number; conflicts: number; fetched: number; error: string | null; retry_after: number }; media?: Record<string, number>; conflicts?: number; error?: string | null; connection_state?: string };

export default function ChatSyncPanel({ chatId, onSources }: { chatId: string; onSources: () => void }) {
  const [status, setStatus] = useState<Status | null>(null);
  const op = useDialogOperation();
  const busy = useRef(false);
  busy.current = op.busy;
  const url = `/api/chats/${encodeURIComponent(chatId)}`;
  useEffect(() => {
    let alive = true, pending = false;
    const poll = async () => {
      if (pending || busy.current) return;
      pending = true;
      const current = op.guard();
      try { const value = await api<Status>(`${url}/telegram`); if (alive && current() && !busy.current) setStatus(value); }
      catch (error) { if (alive && current()) op.setError(error instanceof Error ? error.message : t('Ошибка соединения.')); }
      finally { pending = false; }
    };
    void poll(); const timer = window.setInterval(() => void poll(), 2000);
    return () => { alive = false; window.clearInterval(timer); };
  }, [url]);
  const change = (changes: Record<string, unknown>) => void op.run(async current => {
    if (!status?.binding) return;
    const value = await api<Status>(`${url}/sync-settings`, { method: 'PATCH', body: JSON.stringify({ expected_revision: status.binding.revision, ...changes }) });
    if (current()) setStatus(value);
  });
  const action = (path: string, method = 'POST') => void op.run(async current => {
    if (method === 'DELETE' && !window.confirm(t('Отключить источник Telegram для этого диалога? Сообщения и фотографии сохранятся.'))) return;
    await api(`${url}/${path}`, { method, ...(method === 'DELETE' ? { body: JSON.stringify({ expected_revision: status?.binding?.revision }) } : {}) });
    if (current()) setStatus(await api<Status>(`${url}/telegram`));
  });
  const binding = status?.binding;
  const failed = status?.media?.failed || 0;
  return <section className="index-card telegram-sync-card">
    <h3>{t('Обновление из Telegram')}</h3>
    {!status ? <p>{t('Проверяем…')}</p> : !binding ? <><p>{t('Подключите аккаунт и выберите этот диалог в разделе «Источники».')}</p><div className="dialog-actions"><button onClick={onSources}>{t('Открыть источники')}</button></div></> : <>
      <p role="status">{!binding.enabled ? t('Синхронизация на паузе') : <>
        {status.connection_state && t(telegramStates[status.connection_state] || status.connection_state)}
        {status.connection_state && ' · '}{t(telegramStates[status.run?.state || 'queued'] || status.run?.state || '')}
      </>}</p>
      <div className="telegram-form">
        <label className="check-label"><input type="checkbox" checked={!!binding.enabled} disabled={op.busy} onChange={event => change({ enabled: event.target.checked })} />{t('Получать новые сообщения автоматически')}</label>
        <label className="check-label"><input type="checkbox" checked={!!binding.download_media} disabled={op.busy} onChange={event => change({ download_media: event.target.checked })} />{t('Загружать новые фотографии')}</label>
        <label>{t('Если сообщение удалено в Telegram')}<select aria-label={t('Если сообщение удалено в Telegram')} value={binding.deletion_policy} disabled={op.busy} onChange={event => {
          if (event.target.value === 'mirror' && !window.confirm(t('Удалённые в Telegram сообщения будут удаляться из локального поиска. Исходная выгрузка сохраняется.'))) return;
          change({ deletion_policy: event.target.value });
        }}><option value="archive">{t('Сохранять архивную копию')}</option><option value="mirror">{t('Удалять из локального поиска')}</option></select></label>
        <label>{t('Проверять редакции за последние дни')}<select aria-label={t('Проверять редакции за последние дни')} value={binding.reconcile_days} disabled={op.busy} onChange={event => change({ reconcile_days: Number(event.target.value) })}>{[1, 3, 7, 14, 30].map(days => <option value={days} key={days}>{days}</option>)}</select></label>
      </div>
      <dl className="diagnostics"><dt>{t('Последнее получение сообщений')}</dt><dd>{binding.last_success_at ? new Date(binding.last_success_at * 1000).toLocaleString(uiLocale()) : t('Ещё не завершено')}</dd>
        <dt>{t('История проверена до ID')}</dt><dd>{status.cursor?.scanned_through_id ?? '—'}</dd>
        <dt>{t('Фотографии: готовы / в очереди / ошибки')}</dt><dd>{status.media?.ready || 0} / {status.media?.pending || 0} / {failed}</dd>
        <dt>{t('Последний проход: новых / редакций')}</dt><dd>{status.run?.added || 0} / {status.run?.updated || 0}</dd></dl>
      {status.cursor?.coverage === 'partial_export' && <p className="baseline-note">{t('История до сообщения #{p0} взята из выгрузки; полнота не проверена.', { p0: status.cursor.baseline_id })}</p>}
      {(status.cursor?.upper_bound != null || status.cursor?.recent_upper_bound != null) && <p>{t('Проверка диапазона ещё не завершена. Приложение продолжит с сохранённого места.')}</p>}
      <p className="baseline-note">{t('Готовность текстового, OCR и визуального индексов показана выше отдельно.')}</p>
      {(status.conflicts || 0) > 0 && <p className="warning">{t('Неподтверждённых редакций: {p0}. Текущая версия сохранена.', { p0: status.conflicts! })}</p>}
      {failed > 0 && <p className="warning">{t('Не загружено фотографий: {p0}. Нажмите «Повторить ошибки».', { p0: failed })}</p>}
      {(status.error || status.run?.error) && <p className="error" role="alert">{t(status.error || status.run?.error)}</p>}
      {!!status.run?.retry_after && status.run.retry_after > Date.now() / 1000 && <p>{t('Следующая попытка: {p0}', { p0: new Date(status.run.retry_after * 1000).toLocaleTimeString(uiLocale()) })}</p>}
      <div className="dialog-actions"><button className="primary" disabled={op.busy || !binding.enabled} onClick={() => action('sync')}>{t('Обновить сейчас')}</button>
        <button disabled={op.busy} onClick={() => change({ enabled: !binding.enabled })}>{binding.enabled ? t('Пауза синхронизации') : t('Продолжить синхронизацию')}</button>
        <button disabled={op.busy || !binding.enabled} onClick={() => action('sync/retry')}>{t('Повторить ошибки')}</button>
        <button disabled={op.busy} onClick={() => action('telegram-binding', 'DELETE')}>{t('Отключить источник')}</button></div>
    </>}{op.error && <p className="error" role="alert">{t(op.error)}</p>}
  </section>;
}
