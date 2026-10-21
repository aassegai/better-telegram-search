import { t } from './i18n';
import type { MediaStatus } from './types';

type Wait = NonNullable<MediaStatus['memory_wait']>;

export default function MemoryWaitNotice({ wait, stages, onSettings }: {
  wait?: Wait | null; stages: Wait['stages']; onSettings?: () => void;
}) {
  if (!wait || !wait.stages.some(stage => stages.includes(stage))) return null;
  const mib = (bytes: number) => Math.ceil(bytes / 1024 ** 2);
  return <div className="memory-wait" role="status">
    <p className="warning">{t('RAM приложения: {p0} МиБ · порог: {p1} МиБ.', { p0: mib(wait.rss_bytes), p1: mib(wait.limit_bytes) })}</p>
    {wait.available_bytes != null && <p>{t('Свободно в системе: {p0} МиБ.', { p0: mib(wait.available_bytes) })}</p>}
    <p className="baseline-note">{t('Порог учитывает приложение и OCR-процессы. Приостановите индексацию и увеличьте порог RAM в общих ресурсах индексации. Готовые индексы сохранятся.')}</p>
    {onSettings && <button onClick={onSettings}>{t('Открыть общие настройки')}</button>}
  </div>;
}
