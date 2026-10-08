import { useEffect, useState } from 'react';
import { api } from './api';
import { t } from './i18n';
import type { Mutation } from './IndexCard';
import type { ChatIndexStatus } from './types';
import { useDialogOperation } from './useDialogOperation';

type Props = Mutation & { chatId: string; status: ChatIndexStatus | null; onSaved: (value: ChatIndexStatus) => void };

export default function ChunkingSettings({ chatId, status, pending, onStart, onEnd, onSaved }: Props) {
  const saved = status?.chunking?.profile ?? 'legacy';
  const [profile, setProfile] = useState(saved);
  const [dirty, setDirty] = useState(false);
  const op = useDialogOperation();
  useEffect(() => { if (!dirty) setProfile(saved); }, [saved, dirty]);
  const save = () => void op.run(async current => {
    onStart?.();
    try {
      const value = await api<ChatIndexStatus>(`/api/chats/${encodeURIComponent(chatId)}/index/settings`, {
        method: 'PATCH', body: JSON.stringify({ chunking_profile: profile }),
      });
      if (current()) { setDirty(false); onSaved(value); }
    } finally { onEnd?.(); }
  });
  return <section className="index-card chunking-settings">
    <details><summary>{t('Формирование текстовых фрагментов')}</summary>
      <label>{t('Правила чанкинга')}<select aria-label={t('Правила чанкинга')} value={profile} disabled={!status || pending || op.busy} onChange={event => { setProfile(event.target.value); setDirty(true); }}>
        <option value="legacy">{t('Исходные окна')}</option>
        <option value="episodes">{t('Диалог с фильтрацией однословного шума')}</option>
      </select></label>
      <p className="baseline-note">{profile === 'legacy' ? t('До 8 сообщений и 480 токенов. Короткие реплики расходуют лимит сообщений; однословный шум не фильтруется.') : t('До 8 содержательных сообщений и 480 токенов. Короткие реплики и контекст расходуют токены, но не лимит сообщений. Подписи с содержанием считаются обычными сообщениями.')}</p>
      <p className="baseline-note">{t('Новые правила сохраняют исходную переписку и поиск по словам. Смена правил перестраивает только текстовый индекс этого диалога; паузы, картинки и распознанный OCR сохраняются.')}</p>
      {status?.chunking && <p className="baseline-note">{t('Пропущено в смысловом индексе: {p0}', { p0: status.chunking.skipped_messages })}</p>}
      <button type="button" className="primary" disabled={!status || pending || op.busy || profile === saved} onClick={save}>{op.busy ? t('Сохраняем…') : t('Применить и переиндексировать текст')}</button>
      {op.error && <p className="error" role="alert">{t(op.error)}</p>}
    </details>
  </section>;
}
