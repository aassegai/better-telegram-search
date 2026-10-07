import { api } from './api';
import { t } from './i18n';
import type { SemanticStatus } from './types';
import { useDialogOperation } from './useDialogOperation';
import IndexCard from './IndexCard';
import type { Mutation } from './IndexCard';

export default function TextIndexPanel({ status, onChange, chatId, onStart, onEnd, pending, onModels }: Mutation & {
  status: SemanticStatus | null; onChange: (value: SemanticStatus) => void;
  chatId: string; onModels: () => void;
}) {
  const op = useDialogOperation();
  if (!status) return <p>{t('Проверяем…')}</p>;
  const control = (action: string) => void op.run(async current => {
    onStart?.();
    try {
      const value = await api<{ semantic: SemanticStatus }>(`/api/chats/${encodeURIComponent(chatId)}/index/text/${action}`, { method: 'POST' });
      if (current()) onChange(value.semantic);
    } finally { onEnd?.(); }
  });
  return <IndexCard title={t('Текст')} model="E5" ready={status.ready_segments} total={status.total_segments}
    paused={Boolean(status.paused)} enabled={status.enabled === 1}
    preparing={['downloading', 'preparing'].includes(status.preparation_state)}
    seconds={status.estimated_remaining_seconds} batch={status.batch_size ?? 4} kind="embedding_batch"
    chatId={chatId} busy={op.busy || Boolean(pending)} onPrepare={onModels} onControl={control}
    onSaved={value => onChange((value as { semantic: SemanticStatus }).semantic)} onStart={onStart} onEnd={onEnd}>
    {status.error && <p className="warning" role="alert">{t(status.error)}</p>}
    {op.error && <p className="error" role="alert">{t(op.error)}</p>}
  </IndexCard>;
}
