import { useEffect, useState } from 'react';
import { api } from './api';
import { t } from './i18n';
import type { Chat } from './types';
import SourcePanel from './SourcePanel';
import StoragePanel from './StoragePanel';
import { useDialogOperation } from './useDialogOperation';

export default function DatabasePanel({ chats }: { chats: Chat[] }) {
  const [diagnostics, setDiagnostics] = useState<Record<string, unknown> | null>(null);
  const [chatId, setChatId] = useState(chats[0]?.id ?? '');
  const selectedChat = chats.some(chat => chat.id === chatId) ? chatId : chats[0]?.id ?? '';
  const op = useDialogOperation();
  useEffect(() => {
    let alive = true;
    api<Record<string, unknown>>('/api/doctor').then(value => { if (alive) setDiagnostics(value); })
      .catch(error => { if (alive) op.setError(error.message); });
    return () => { alive = false; };
  }, []);
  return <>
    <h3>{t('Состояние базы')}</h3>
    {diagnostics ? <dl className="diagnostics"><dt>{t('База')}</dt><dd>{diagnostics.database_check === 'ok' ? t('Исправна') : t('Требует проверки')}</dd>
      <dt>{t('Сообщений')}</dt><dd>{String(diagnostics.messages)}</dd><dt>{t('Сегменты в очереди индекса')}</dt><dd>{String(diagnostics.pending_index_segments)}</dd>
      <dt>{t('Доступно памяти')}</dt><dd>{(Number(diagnostics.ram_available_bytes) / 1024 ** 3).toFixed(1)} {t('ГиБ')}</dd>
      <dt>{t('Свободно на диске')}</dt><dd>{(Number(diagnostics.disk_free_bytes) / 1024 ** 3).toFixed(1)} {t('ГиБ')}</dd></dl> : <p>{t('Проверяем…')}</p>}
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
