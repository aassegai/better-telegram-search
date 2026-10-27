import { useEffect, useState } from 'react';
import { api } from './api';
import { t } from './i18n';
import type { Chat } from './types';
import SourcePanel from './SourcePanel';
import StoragePanel from './StoragePanel';
import { useDialogOperation } from './useDialogOperation';

type Diagnostics = Record<string, unknown> & { diagnostic_snapshot?: { state: string; partial: boolean } };
const number = (value: unknown) => typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : null;
const count = (value: unknown) => number(value) == null ? '—' : String(value);
const gib = (value: unknown) => number(value) == null ? '—' : `${(Number(value) / 1024 ** 3).toFixed(1)} ${t('ГиБ')}`;

export default function DatabasePanel({ chats }: { chats: Chat[] }) {
  const [diagnostics, setDiagnostics] = useState<Diagnostics | null>(null);
  const [refreshId, setRefreshId] = useState(0);
  const [loading, setLoading] = useState(false);
  const [pollingStopped, setPollingStopped] = useState(false);
  const [chatId, setChatId] = useState(chats[0]?.id ?? '');
  const selectedChat = chats.some(chat => chat.id === chatId) ? chatId : chats[0]?.id ?? '';
  const op = useDialogOperation();
  useEffect(() => {
    let alive = true;
    let timer: ReturnType<typeof setTimeout>;
    let controller: AbortController | null = null;
    let attempts = 0;
    setPollingStopped(false);
    const poll = async () => {
      controller = new AbortController();
      const request = controller;
      const timeout = setTimeout(() => request.abort(), 5000);
      attempts++;
      setLoading(true);
      try {
        const value = await api<Diagnostics>('/api/doctor', { signal: request.signal });
        if (!alive) return;
        setDiagnostics(value); op.setError('');
        const state = value.diagnostic_snapshot?.state;
        if (state && !['ready', 'failed'].includes(state)) {
          if (attempts < 15) timer = setTimeout(() => void poll(), 2000);
          else setPollingStopped(true);
        }
      } catch (error) {
        if (alive) op.setError(request.signal.aborted ? t('Ошибка соединения.') : error instanceof Error ? error.message : t('Ошибка операции.'));
      } finally { clearTimeout(timeout); if (alive) setLoading(false); }
    };
    void poll();
    return () => { alive = false; clearTimeout(timer); controller?.abort(); };
  }, [refreshId]);
  const state = diagnostics?.diagnostic_snapshot?.state;
  const checking = diagnostics?.database_check === 'checking' || diagnostics?.database_check == null;
  return <>
    <h3>{t('Состояние базы')}</h3>
    {diagnostics ? <dl className="diagnostics"><dt>{t('База')}</dt><dd>{diagnostics.database_check === 'ok' ? t('Исправна') : checking ? t('Проверяем…') : t('Требует проверки')}</dd>
      <dt>{t('Сообщений')}</dt><dd>{count(diagnostics.messages)}</dd><dt>{t('Сегменты в очереди индекса')}</dt><dd>{count(diagnostics.pending_index_segments)}</dd>
      <dt>{t('Доступно памяти')}</dt><dd>{gib(diagnostics.ram_available_bytes)}</dd>
      <dt>{t('Свободно на диске')}</dt><dd>{gib(diagnostics.disk_free_bytes)}</dd></dl> : <p>{t('Проверяем…')}</p>}
    {state === 'waiting' && <p role="status">{t('Подробная диагностика ожидает завершения текущей операции.')}</p>}
    {state === 'collecting' && <p role="status">{t('Собираем подробную диагностику…')}</p>}
    {state === 'failed' && <p className="error" role="alert">{t('Не удалось собрать подробную диагностику. Повторите позже.')}</p>}
    {pollingStopped && <p role="status">{t('Диагностика ещё собирается. Обновите сведения позже.')}</p>}
    <div className="job-actions"><button disabled={loading} onClick={() => setRefreshId(value => value + 1)}>{t('Обновить диагностику')}</button></div>
    <p className="baseline-note">{t('База хранится локально без шифрования.')}</p>
    {op.error && <p className="error" role="alert">{t(op.error)}</p>}
    <StoragePanel />
    {chats.length > 0 && <section className="database-sources"><h3>{t('Локальные источники')}</h3>
      <label>{t('Диалог')}<select aria-label={t('Диалог источников')} value={selectedChat} onChange={event => setChatId(event.target.value)}>
        {chats.map(chat => <option key={chat.id} value={chat.id}>{chat.name}</option>)}</select></label>
      {selectedChat && <SourcePanel key={selectedChat} chatId={selectedChat} />}
    </section>}
  </>;
}
