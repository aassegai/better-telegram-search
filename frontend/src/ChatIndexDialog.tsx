import { useEffect, useRef, useState } from 'react';
import { api } from './api';
import { t } from './i18n';
import type { Chat, MediaStatus, SemanticStatus } from './types';
import TextIndexPanel from './TextIndexPanel';
import WorkspacePanel from './WorkspacePanel';
import SourcePanel from './SourcePanel';

type Status = { semantic: SemanticStatus; media: MediaStatus };

export default function ChatIndexDialog({ chat, onClose, onModels }: { chat: Chat; onClose: () => void; onModels: () => void }) {
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
  return <section className="modal" role="dialog" aria-modal="true" aria-labelledby="chat-index-title">
    <button className="close" aria-label={t('Закрыть')} onClick={onClose}>×</button>
    <div className="eyebrow">{t('ИНДЕКСАЦИЯ ДИАЛОГА')}</div><h2 id="chat-index-title">{chat.name}</h2>
    {error && <p className="error" role="alert">{t(error)}</p>}
    <TextIndexPanel onModels={onModels} chatId={chat.id} pending={pending} onStart={onStart} onEnd={onEnd} status={status?.semantic ?? null}
      onChange={semantic => setStatus(value => value ? { ...value, semantic } : value)} />
    <WorkspacePanel onModels={onModels} chatId={chat.id} pending={pending} onStart={onStart} onEnd={onEnd} indexing media={status?.media ?? null}
      onMediaChange={media => setStatus(value => value ? { ...value, media } : value)} />
    <SourcePanel chatId={chat.id} />
  </section>;
}
