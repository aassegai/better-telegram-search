import { t } from './i18n';
import { api } from './api';
import type { MediaStatus } from './types';
import { useDialogOperation } from './useDialogOperation';
import IndexCard from './IndexCard';
import OcrIndexCard from './OcrIndexCard';
import { estimatedTime } from './estimatedTime';
import type { Mutation } from './IndexCard';
import MemoryWaitNotice from './MemoryWaitNotice';

export default function WorkspacePanel({ media, onMediaChange, chatId, onStart, onEnd, pending, onModels, onPerformance, onResources }: Mutation & { media: MediaStatus | null; onMediaChange: (value: MediaStatus) => void; chatId?: string; onModels?: () => void; onPerformance: () => void; onResources: () => void }) {
  const op = useDialogOperation();
  const busy = op.busy || Boolean(pending);
  const accept = (value: unknown) => { const result = value as MediaStatus | { media: MediaStatus }; onMediaChange('media' in result ? result.media : result); };
  const control = (action: string, kind: 'images' | 'ocr' | 'ocr_dense' = 'images') => void op.run(async current => {
    onStart?.();
    try {
      const value = await api(chatId ? `/api/chats/${encodeURIComponent(chatId)}/index/${kind}/${action}` : `/api/${kind === 'ocr' ? 'ocr-index' : kind === 'ocr_dense' ? 'ocr-dense-index' : 'image-index'}/${action}`, { method: 'POST' });
      if (current()) accept(value);
    } finally { onEnd?.(); }
  });
  const preparing = media?.preparation_state === 'preparing';
  return <section className="workspace-panel">

      {media && <IndexCard title={t('Изображения')} model={media.visual_profile === 'siglip2' ? 'SigLIP 2' : 'CLIP'} ready={media.images_ready} total={media.total_photos}
        paused={Boolean(media.paused)} enabled={media.images_enabled === 1} preparing={preparing}
        failed={media.images_failed ?? 0}
        blocked={(media.images_failed ?? 0) > 0 && (Boolean(media.paused) || media.images_ready + (media.images_failed ?? 0) >= media.total_photos)}
        seconds={media.images_estimated_remaining_seconds} estimate={media.images_estimate} batch={media.batch_size ?? 1}
        chatId={chatId} busy={busy} onPrepare={onModels ?? (() => {})} onControl={control}
        onPerformance={onPerformance}>
        {[media.error, media.memory_wait && t(media.resource_error) === t('Достигнут предел RAM: медиа ожидает освобождения памяти.') ? null : media.resource_error].filter(Boolean).map(value => <p className="warning" key={value}>{t(value)}</p>)}
        <MemoryWaitNotice wait={media.memory_wait} stages={['clip']} onSettings={onResources} />
        {media.missing_refs > 0 && <p className="warning">{t('Недоступных фотографий в источниках: ')}{media.missing_refs}</p>}
      </IndexCard>}
      {media && <OcrIndexCard media={media} busy={busy} preparing={preparing} onModels={onModels ?? (() => {})} onResources={onResources} onControl={action => control(action, 'ocr')} onDenseControl={action => control(action, 'ocr_dense')}>
        <button type="button" className="text-button" onClick={onPerformance}>{t('Настройки производительности')}</button>
      </OcrIndexCard>}
      {media?.queues && <details className="index-advanced"><summary>{t('Скорость и очереди подготовки')}</summary>
        <p>{t('Уникальных доступных изображений: {p0}', { p0: media.total_photos })}</p>
        {media.photo_attachments != null && <p>{t('Вложений: {p0}. Сообщений с фото: {p1}.', { p0: media.photo_attachments, p1: media.photo_messages ?? 0 })}</p>}
        <p>{t('Вся подготовка: ')}{estimatedTime(media.preparation_estimated_remaining_seconds, t('Собираем статистику для оценки времени.'), media.preparation_estimate)}</p>
        {(['ocr', 'clip', 'ocr_dense'] as const).map(stage => { const queue = media.queues?.[stage]; return queue && <p key={stage}>{stage === 'ocr' ? 'OCR' : stage === 'clip' ? (media.visual_profile === 'siglip2' ? 'SigLIP 2' : 'CLIP') : t('OCR → текстовая модель')}: {t('В очереди: {p0}', { p0: queue.pending })} · {queue.units_per_minute == null ? t('Измеряем скорость…') : t('{p0} изобр./мин.', { p0: queue.units_per_minute })}</p>; })}
        <small>{t('Оценка учитывает недавние батчи и параллельную работу этапов. До завершения распознавания объём смыслового OCR оценивается предварительно.')}</small>
      </details>}

    {op.error && <p className="error" role="alert">{t(op.error)}</p>}
  </section>;
}
