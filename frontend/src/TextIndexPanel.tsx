import { api } from './api';
import { t } from './i18n';
import type { SemanticStatus } from './types';
import { useDialogOperation } from './useDialogOperation';
import IndexCard from './IndexCard';
import type { Mutation } from './IndexCard';
import { useIndexWorkerHealth } from './useIndexWorkerHealth';

export default function TextIndexPanel({ status, onChange, chatId, onStart, onEnd, pending, onModels, onPerformance }: Mutation & {
  status: SemanticStatus | null; onChange: (value: SemanticStatus) => void;
  chatId: string; onModels: () => void; onPerformance: () => void;
}) {
  const op = useDialogOperation();
  const live = useIndexWorkerHealth();
  const worker = live ?? status?.worker;
  const waiting = Boolean(worker?.enabled && worker.waiting_reason === 'database');
  const workerFault = Boolean(worker?.enabled && !worker.stopped && (!worker.alive || worker.phase === 'error'));
  const control = (action: string) => void op.run(async current => {
    onStart?.();
    try {
      const value = await api<{ semantic: SemanticStatus }>(`/api/chats/${encodeURIComponent(chatId)}/index/text/${action}`, { method: 'POST' });
      if (current()) onChange(value.semantic);
    } finally { onEnd?.(); }
  });
  const notice = <>
    {waiting && <p className="warning" role="status">{t('База данных занята. Индексация продолжится автоматически.')}</p>}
    {workerFault && <><p className="warning" role="alert">{t(worker?.alive ? 'Фоновый индексатор сообщил об ошибке.' : 'Фоновый индексатор остановлен. Запустите его снова.')}</p>
      <button type="button" className="primary" disabled={op.busy || pending} onClick={() => control('retry')}>{t('Перезапустить индексатор')}</button></>}
  </>;
  if (!status) return <>{notice}<p>{t('Проверяем…')}</p>{op.error && <p className="error" role="alert">{t(op.error)}</p>}</>;
  const failed = status.works.filter(work => work.state === 'failed').reduce((sum, work) => sum + work.count, 0);
  const active = status.works.some(work => ['pending', 'running'].includes(work.state) && work.count > 0);
  const indexError = status.index_error ?? status.error;
  const blocked = failed > 0 && (Boolean(status.paused) || !active);
  return <IndexCard title={t('Текст')} model={status.profile === 'berta' ? 'BERTA' : status.profile === 'ultrabert' ? 'UltraBERT' : 'E5'} ready={status.ready_segments} total={status.total_segments}
    paused={Boolean(status.paused)} enabled={status.enabled === 1}
    preparing={['downloading', 'preparing'].includes(status.preparation_state)}
    blocked={blocked || workerFault} failed={failed}
    seconds={waiting ? null : status.estimated_remaining_seconds} estimate={waiting ? { ...status.estimate, lower_seconds: null, upper_seconds: null, waiting_reason: 'database' } : status.estimate} batch={status.batch_size ?? 4}
    chatId={chatId} busy={op.busy || Boolean(pending)} onPrepare={onModels} onControl={control}
    onPerformance={onPerformance}>
    {notice}
    {indexError && <p className="warning" role="alert">{t(indexError)}</p>}
    {op.error && <p className="error" role="alert">{t(op.error)}</p>}
  </IndexCard>;
}
