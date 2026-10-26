import { useEffect, useRef, useState } from 'react';
import { api } from './api';
import { t } from './i18n';
import type { Chat, ChatIndexStatus } from './types';
import IndexExclusions from './IndexExclusions';
import ChatSyncPanel from './ChatSyncPanel';
import TextIndexPanel from './TextIndexPanel';
import WorkspacePanel from './WorkspacePanel';
import SourcePanel from './SourcePanel';
import SettingsDialog from './SettingsDialog';
import ChatVectorStorage from './ChatVectorStorage';

type Status = ChatIndexStatus;

export default function ChatIndexDialog({ chat, onClose, onModels, onSources, onPerformance, onResources }: { chat: Chat; onClose: () => void; onModels: () => void; onSources: () => void; onPerformance: () => void; onResources: () => void }) {
  const [status, setStatus] = useState<Status | null>(null);
  const [error, setError] = useState('');
  const revision = useRef(0);
  const mutations = useRef(0);
  const [pending, setPending] = useState(false);
  const onStart = () => { revision.current++; mutations.current++; setPending(true); };
  const onEnd = () => { revision.current++; mutations.current--; setPending(mutations.current > 0); };
  useEffect(() => {
    let alive = true;
    let pending = false;
    const poll = async () => {
      if (pending || mutations.current) return;
      pending = true;
      const started = revision.current;
      try {
        const value = await api<Status>(`/api/chats/${encodeURIComponent(chat.id)}/index`);
        if (alive && started === revision.current && !mutations.current) { setStatus(value); setError(''); }
      } catch (error) { if (alive && started === revision.current && !mutations.current) setError(error instanceof Error ? error.message : t('Ошибка соединения.')); }
      finally { pending = false; }
    };
    void poll();
    const timer = window.setInterval(() => void poll(), 2000);
    return () => { alive = false; window.clearInterval(timer); };
  }, [chat.id]);
  return <SettingsDialog titleId="chat-index-title" onClose={onClose}>
    <h2 id="chat-index-title">{chat.name}</h2>
    <button type="button" className="secondary" onClick={onPerformance}>{t('Производительность этого диалога')}</button>
    {error && <p className="error" role="alert">{t(error)}</p>}
    <IndexExclusions chatId={chat.id} status={status} pending={pending} onStart={onStart} onEnd={onEnd} onSaved={setStatus} />
    <TextIndexPanel onModels={onModels} onPerformance={onPerformance} chatId={chat.id} pending={pending} onStart={onStart} onEnd={onEnd} status={status?.semantic ?? null}
      onChange={semantic => setStatus(value => value ? { ...value, semantic } : value)} />
    <WorkspacePanel onModels={onModels} onPerformance={onPerformance} onResources={onResources} chatId={chat.id} pending={pending} onStart={onStart} onEnd={onEnd} media={status?.media ?? null}
      onMediaChange={media => setStatus(value => value ? { ...value, media } : value)} />
    <ChatVectorStorage chat={chat} pending={pending} onStart={onStart} onEnd={onEnd} />
    <ChatSyncPanel chatId={chat.id} onSources={onSources} />
    <SourcePanel chatId={chat.id} />
  </SettingsDialog>;
}
