import { t } from './i18n';
import { estimatedTime } from './estimatedTime';
import type { MediaStatus } from './types';
import IndexErrors from './IndexErrors';
import type { ReactNode } from 'react';

type Props = {
  media: MediaStatus; busy: boolean; preparing: boolean;
  onModels: () => void; onControl: (action: string) => void;
  onDenseControl: (action: string) => void; children?: ReactNode;
};

export default function OcrIndexCard({ media, busy, preparing, onModels, onControl, onDenseControl, children }: Props) {
  const enabled = media.ocr_enabled === 1;
  const completed = media.ocr_ready + media.ocr_failed;
  const done = completed >= media.total_photos;
  const failed = media.ocr_failed;
  const denseFailed = media.ocr_dense_failed ?? 0;
  const densePending = (media.ocr_nonempty_ready ?? 0) - media.ocr_dense_ready - denseFailed;
  const denseEta = densePending <= 0 ? denseFailed > 0 ? t('Обработка завершена. Ошибки нужно повторить.') : done ? t('Смысловая индексация OCR завершена.') : t('Ждём новые распознанные изображения.') : estimatedTime(media.ocr_dense_estimated_remaining_seconds, t('Оценка смыслового OCR появится после первых изображений.'));
  const paused = media.ocr_paused ?? media.paused;
  const status = preparing ? t('Подготавливаем…') : !enabled ? t('Не подготовлен') : done ?
    failed > 0 ? t('Требуется повтор') : t('Готово') : paused ? t('На паузе') : t('Распознавание');
  return <section className="index-card" aria-label={t('Текст на изображениях · OCR')}>
    <header className="index-card-heading"><h3>{t('Текст на изображениях')}</h3><span className="index-model">OCR · {(media.ocr_backend?.device ?? 'cpu').toUpperCase().split('+').join(' + ')}</span></header>
    <div className="index-progress-label"><span>{t('Обработано: {p0} / {p1}', { p0: completed, p1: media.total_photos })}</span><span>{status}</span></div>
    <progress aria-label={t('Прогресс распознавания OCR')} value={completed} max={Math.max(1, media.total_photos)} />
    {enabled && <p className="index-eta" role="status">{done ? media.ocr_failed > 0 ? t('Обработка завершена. Ошибки нужно повторить.') : t('Распознавание завершено.') : estimatedTime(media.ocr_estimated_remaining_seconds, t('Оценка появится после первых изображений.'))}</p>}
    <IndexErrors count={failed} busy={busy || preparing || !enabled} onRetry={() => onControl('retry')}>
      {media.ocr_failed > 0 && <p>{t('OCR с ошибкой: ')}{media.ocr_failed}</p>}
      {media.ocr_failed > 0 && <p>{t('Успешно распознано: {p0}', { p0: media.ocr_ready })}</p>}
    </IndexErrors>
    {enabled && media.ocr_dense_available && <div className="ocr-semantic-progress">
      <p>{t('OCR по смыслу: {p0} / {p1} с текстом', { p0: media.ocr_dense_ready, p1: media.ocr_nonempty_ready ?? 0 })}</p>
      <progress aria-label={t('Прогресс смысловой индексации OCR')} value={media.ocr_dense_ready} max={Math.max(1, media.ocr_nonempty_ready ?? 0)} />
      <p className="index-eta">{denseEta}</p>
      <small>{t('Распознанный текст уже доступен для поиска по словам. Смысловой индекс готовится отдельно.')}</small>
      <IndexErrors count={media.ocr_dense_failed ?? 0} busy={busy || preparing} onRetry={() => onDenseControl('retry')}>
        <p>{t('Смысловой OCR с ошибкой: {p0}', { p0: media.ocr_dense_failed ?? 0 })}</p>
      </IndexErrors>
      <div className="job-actions index-actions"><button disabled={busy || preparing} onClick={() => onDenseControl(media.ocr_dense_paused ? 'resume' : 'pause')}>{media.ocr_dense_paused ? t('Продолжить смысловой OCR') : t('Пауза смыслового OCR')}</button>
      </div>
    </div>}
    {!enabled && <div className="job-actions index-actions"><button className="primary" disabled={busy || preparing} onClick={onModels}>{t('Открыть настройки моделей')}</button></div>}
    {enabled && <div className="job-actions index-actions">
      <button className="primary" disabled={busy || preparing} onClick={() => onControl(paused ? 'resume' : 'pause')}>{paused ? t('Продолжить OCR') : t('Пауза OCR')}</button>
    </div>}
    {children}
  </section>;
}
