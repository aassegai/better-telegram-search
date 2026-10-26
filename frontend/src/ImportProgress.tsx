import { t, uiLocale } from './i18n';
import type { ImportTaskProgress } from './types';

const phases: Record<string, string> = {
  queued: 'Ожидание в очереди', metadata: 'Чтение метаданных и хеширование JSON',
  verify_json: 'Проверка неизменности JSON', resume_scan: 'Переход к сохранённой позиции',
  checking_messages: 'Проверка сообщений и вложений', writing_messages: 'Запись сообщений',
  checking_media: 'Проверка вложения', preview_ready: 'Проверка завершена',
  import_queued: 'Импорт поставлен в очередь', import_completed: 'Импорт завершён',
};
const states: Record<string, string> = {
  paused: 'На паузе', interrupted: 'Прервано · можно продолжить', failed: 'Ошибка', cancelled: 'Отменено',
  ready: 'Проверка завершена', completed: 'Импорт завершён', applied: 'Импорт поставлен в очередь',
};
const number = (value: number) => value.toLocaleString(uiLocale(), { maximumFractionDigits: 1 });
const bytes = (value: number) => t('{p0} МиБ', { p0: number(value / 1024 ** 2) });

export default function ImportProgress({ progress, state, compact = false }: {
  progress?: ImportTaskProgress | null; state: string; compact?: boolean;
}) {
  const queued = state === 'queued';
  const known = !queued && progress?.total != null && progress.total > 0;
  const active = queued || state === 'running';
  const label = states[state] ?? phases[queued ? 'queued' : progress?.phase ?? ''] ?? 'Обработка…';
  return <section className={`import-progress ${compact ? 'compact' : ''}`} aria-label={t('Ход импорта')}>
    <div className="import-progress-heading"><span role="status">{t(label)}</span></div>
    {(known || active) && <progress aria-label={t('Прогресс этапа импорта')} max={known ? progress!.total! : undefined}
      value={known ? progress!.completed : undefined} />}
    {!queued && progress && <div className="import-progress-measurements">
      <span>{progress.unit === 'bytes'
        ? progress.total == null ? bytes(progress.completed) : t('{p0} / {p1}', { p0: bytes(progress.completed), p1: bytes(progress.total) })
        : progress.total == null ? t('Обработано: {p0}', { p0: number(progress.completed) })
          : t('{p0} / {p1} сообщений', { p0: number(progress.completed), p1: number(progress.total) })}</span>
      {!compact && <>
        <span>{t('Прошло {p0} с', { p0: Math.floor(progress.elapsed_seconds) })}</span>
        {state === 'running' && progress.rate != null && <span>{progress.unit === 'bytes'
          ? t('{p0} МиБ/с', { p0: number(progress.rate / 1024 ** 2) })
          : t('{p0} сообщений/с', { p0: number(progress.rate) })}</span>}
        {state === 'running' && progress.eta_seconds != null && <span>{t('До конца этапа: около {p0} с', { p0: Math.ceil(progress.eta_seconds) })}</span>}
      </>}
    </div>}
    {state === 'running' && progress?.activity && <div className="import-activity">
      <small>{t(phases[progress.activity.phase] ?? 'Обработка…')}
        {progress.activity.unit === 'bytes' && <> · {bytes(progress.activity.completed)}{progress.activity.total != null && <> / {bytes(progress.activity.total)}</>}</>}</small>
      {!compact && <progress aria-label={t('Проверка текущего вложения')} max={progress.activity.total || undefined}
        value={progress.activity.total ? progress.activity.completed : undefined} />}
    </div>}
  </section>;
}
