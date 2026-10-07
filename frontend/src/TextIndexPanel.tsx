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
  const failed = status.works.filter(work => work.state === 'failed').reduce((sum, work) => sum + work.count, 0);
  const active = status.works.some(work => ['pending', 'running'].includes(work.state) && work.count > 0);
  const indexError = status.index_error ?? status.error;
  const blocked = failed > 0 && (Boolean(status.paused) || !active);
  return <IndexCard title={t('Текст')} model="E5" ready={status.ready_segments} total={status.total_segments}
    paused={Boolean(status.paused)} enabled={status.enabled === 1}
    preparing={['downloading', 'preparing'].includes(status.preparation_state)}
    blocked={blocked}
    seconds={status.estimated_remaining_seconds} batch={status.batch_size ?? 4} kind="embedding_batch"
    chatId={chatId} busy={op.busy || Boolean(pending)} onPrepare={onModels} onControl={control}
    onSaved={value => onChange((value as { semantic: SemanticStatus }).semantic)} onStart={onStart} onEnd={onEnd}>
    {failed > 0 && <p className="warning">{t('Задач с ошибкой: {p0}', { p0: failed })}</p>}
    {indexError && <p className="warning" role="alert">{t(indexError)}</p>}
    {op.error && <p className="error" role="alert">{t(op.error)}</p>}
  </IndexCard>;
}
