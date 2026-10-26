import { useEffect, useState } from 'react';
import { api } from './api';
import { t } from './i18n';
import BatchFields, { validBatches } from './BatchFields';
import type { Batches, Overrides } from './BatchFields';
import { useDialogOperation } from './useDialogOperation';

type Performance = { batch_defaults: Batches; batch_overrides: Overrides; batches: Batches };

export default function ChatPerformancePanel({ chatId }: { chatId: string }) {
  const [settings, setSettings] = useState<Performance | null>(null);
  const [draft, setDraft] = useState<Overrides | null>(null);
  const [notice, setNotice] = useState('');
  const op = useDialogOperation();
  useEffect(() => {
    let alive = true;
    api<Performance>(`/api/chats/${encodeURIComponent(chatId)}/performance`).then(value => {
      if (alive) { setSettings(value); setDraft(value.batch_overrides); }
    }).catch(error => { if (alive) op.setError(error.message); });
    return () => { alive = false; };
  }, [chatId]);
  const changed = settings && draft && Object.keys(draft).some(key => draft[key as keyof Overrides] !== settings.batch_overrides[key as keyof Overrides]);
  return <section className="performance-chat">
    <p>{t('Эти батчи действуют только для выбранного диалога. Изменения применятся со следующего батча; готовые векторы сохранятся.')}</p>
    {draft && settings && <BatchFields values={draft} defaults={settings.batch_defaults} inherit disabled={op.busy}
      onChange={(key, value) => { setDraft({ ...draft, [key]: value }); setNotice(''); }} />}
    <button className="primary" disabled={!draft || !changed || op.busy || !validBatches(draft, true)} onClick={() => void op.run(async current => {
      if (!draft || !settings) return;
      const changes = Object.fromEntries(Object.entries(draft).filter(([key, value]) => value !== settings.batch_overrides[key as keyof Overrides]));
      const value = await api<Performance>(`/api/chats/${encodeURIComponent(chatId)}/performance`, { method: 'PATCH', body: JSON.stringify(changes) });
      if (current()) { setSettings(value); setDraft(value.batch_overrides); setNotice('Настройки диалога сохранены.'); }
    })}>{op.busy ? t('Сохраняем…') : t('Сохранить настройки диалога')}</button>
    {notice && <p role="status">{t(notice)}</p>}{op.error && <p className="error" role="alert">{t(op.error)}</p>}
  </section>;
}
