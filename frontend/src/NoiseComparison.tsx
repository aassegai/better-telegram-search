import { useEffect, useRef, useState } from 'react';
import { api } from './api';
import { t, uiLocale } from './i18n';
import { noiseLabel } from './noiseTypes';
import { useDialogOperation } from './useDialogOperation';

type Counts = Record<string, number | null>;
type Result = {
  complete: boolean; comparison_complete: boolean; source_verified: boolean;
  limits: string[]; before: Counts | null; after: Counts | null;
  delta: Counts | null; cpu_seconds: number; elapsed_seconds: number;
  process_peak_rss_bytes: number; batch_size: number;
};
type Comparison = {
  id: string; state: string; active: boolean; stale: boolean; error: string | null;
  progress: { phase: string; scanned_messages: number; total_messages?: number | null; elapsed_seconds: number };
  summary: Result | null;
};
const states: Record<string, string> = { queued: 'В очереди', running: 'Сравниваем', cancelling: 'Останавливаем…', cancelled: 'Отменён', ready: 'Сравнение готово', limited: 'Ограниченное сравнение', failed: 'Ошибка сравнения', stale: 'Сравнение устарело', interrupted: 'Прерван перезапуском' };
const phases: Record<string, string> = { queued: 'Ожидаем очередь', preparing: 'Проверяем токенизатор и правила', before: 'Текущие фрагменты', after: 'Предлагаемые фрагменты' };
const errors: Record<string, string> = { source_changed: 'Диалог, правила или модель изменились. Повторите сравнение.', tokenizer_unavailable: 'Подготовьте текстовую модель в настройках приложения. Сравнение использует только её локальный токенизатор.', database_busy: 'База занята. Повторите запрос через несколько секунд.', comparison_failed: 'Не удалось сравнить чанкинг. Проверьте анализ и повторите сравнение.', result_save_failed: 'Не удалось сохранить сравнение. Перезапустите приложение.', interrupted: 'Сравнение прервано перезапуском.' };
const metrics = [['chunks', 'Фрагменты'], ['tokens', 'Токены'], ['own_messages', 'Собственные сообщения']] as const;
const technical = [['padded_token_slots', 'Токены с паддингом'], ['skipped_messages', 'Не вошло в смысловой текст'], ['protected_noise_messages', 'Сохранено защитами']] as const;

export default function NoiseComparison({ base, revision, canStart, parentBusy, onProtectionChange }: {
  base: string; revision: number; canStart: boolean; parentBusy: boolean; onProtectionChange: (value: boolean) => void;
}) {
  const [job, setJob] = useState<Comparison | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [readError, setReadError] = useState('');
  const [epoch, setEpoch] = useState(0);
  const [maxMessages, setMaxMessages] = useState(100000);
  const [timeLimit, setTimeLimit] = useState(300);
  const request = useRef<AbortController | null>(null);
  const op = useDialogOperation();
  useEffect(() => () => { request.current?.abort(); onProtectionChange(false); }, []);
  useEffect(() => { onProtectionChange(!loaded || job?.active === true); }, [loaded, job?.active, onProtectionChange]);
  useEffect(() => {
    let alive = true;
    let timer: ReturnType<typeof setTimeout>;
    const controller = new AbortController();
    const current = op.guard();
    const poll = async () => {
      if (!alive || !current()) return;
      try {
        const value = await api<{ comparison: Comparison | null }>(`${base}/dry-runs`, { signal: controller.signal });
        if (alive && current()) { setJob(value.comparison); setLoaded(true); setReadError(''); }
      } catch (error) {
        if (alive && current() && !controller.signal.aborted) {
          // Keep the known task ID and cancellation while status is temporarily
          // unavailable. Hide unverified results and protect parent deletion.
          setJob(previous => previous?.active ? { ...previous, summary: null } : null);
          setLoaded(false); setReadError(error instanceof Error ? error.message : t('Ошибка соединения.'));
        }
      }
      if (alive && current()) timer = setTimeout(() => void poll(), 1500);
    };
    void poll();
    return () => { alive = false; clearTimeout(timer); controller.abort(); };
  }, [base, revision, epoch]);
  const perform = (cancel = false) => void op.run(async current => {
    const controller = new AbortController(); request.current = controller;
    try {
      const value = await api<Comparison>(`${base}/dry-runs${cancel && job ? `/${job.id}/cancel` : ''}`, {
        method: 'POST', signal: controller.signal,
        ...(!cancel ? { body: JSON.stringify({ revision, max_messages: maxMessages, time_limit_seconds: timeLimit }) } : {}),
      });
      if (current()) setJob(value);
    } catch (error) { if (!controller.signal.aborted) throw error; }
    finally { if (request.current === controller) request.current = null; if (current()) setEpoch(value => value + 1); }
  });
  const valid = Number.isInteger(maxMessages) && maxMessages >= 1 && maxMessages <= 1000000 && Number.isInteger(timeLimit) && timeLimit >= 1 && timeLimit <= 3600;
  const result = job?.summary;
  const count = (value: number | null | undefined) => typeof value === 'number' && Number.isFinite(value) ? value.toLocaleString(uiLocale()) : '—';
  const rows = (items: readonly (readonly [string, string])[]) => items.map(([key, label]) => <tr key={key}><th scope="row">{t(label)}</th><td>{count(result?.before?.[key])}</td><td>{count(result?.after?.[key])}</td></tr>);
  return <section className="noise-comparison" aria-label={t('Сравнение чанкинга')} aria-busy={op.busy}>
    <h4>{t('Сравнение чанкинга')}</h4>
    <p className="baseline-note">{t('CPU-сравнение фрагментов и токенов до применения. Индекс не меняется, модели не скачиваются.')}</p>
    <details><summary>{t('Лимиты сравнения')}</summary>
      <label>{t('Максимум сообщений')}<input type="number" min="1" max="1000000" value={Number.isNaN(maxMessages) ? '' : maxMessages} disabled={op.busy || job?.active} onChange={event => setMaxMessages(event.target.value === '' ? NaN : Number(event.target.value))} /></label>
      <label>{t('Лимит времени, с')}<input type="number" min="1" max="3600" value={Number.isNaN(timeLimit) ? '' : timeLimit} disabled={op.busy || job?.active} onChange={event => setTimeLimit(event.target.value === '' ? NaN : Number(event.target.value))} /></label>
    </details>
    <div className="job-actions">
      <button type="button" disabled={!loaded || !canStart || parentBusy || op.busy || job?.active || !valid} onClick={() => perform()}>{t('Сравнить фрагменты и токены')}</button>
      {job?.active && <button type="button" disabled={op.busy || job.state === 'cancelling'} onClick={() => perform(true)}>{t('Отменить сравнение')}</button>}
    </div>
    {job && <div aria-live="polite">
      <strong>{t(noiseLabel(states, job.state, 'Нет данных'))}</strong>
      {job.active && <>
        <p>{t(noiseLabel(phases, job.progress.phase, 'Сравниваем'))} · {t('Прошло {p0} с', { p0: Math.round(job.progress.elapsed_seconds) })}</p>
        <progress aria-label={t('Прогресс сравнения')} value={job.progress.total_messages ? job.progress.scanned_messages : undefined} max={job.progress.total_messages || 1} />
        <p>{t('Проверено сообщений в проходе: {p0}', { p0: job.progress.scanned_messages })}</p>
      </>}
      {job.error && <p className="warning">{t(noiseLabel(errors, job.error, 'Не удалось сравнить чанкинг. Проверьте анализ и повторите сравнение.'))}</p>}
    </div>}
    {result && !job?.stale && <>
      {!result.complete && <p className="warning">{t('Сравнение ограничено. Результат не описывает весь диалог.')}</p>}
      {result.limits.includes('message_limit') && <p>{t('Сравнены первые {p0} сообщений.', { p0: result.before?.source_messages ?? 0 })}</p>}
      {(!result.comparison_complete || !result.source_verified) && <p className="warning">{t('Сравнение не завершено. Проверенная разница не рассчитана.')}</p>}
      <table><thead><tr><th scope="col">{t('Показатель')}</th><th scope="col">{t('До')}</th><th scope="col">{t('После')}</th></tr></thead><tbody>{rows(metrics)}</tbody></table>
      {result.comparison_complete && result.source_verified && result.delta && <p>{t('Изменение фрагментов: {p0} · токенов: {p1}', { p0: result.delta.chunks ?? 0, p1: result.delta.tokens ?? 0 })}</p>}
      <details><summary>{t('Подробности измерения')}</summary>
        <table><thead><tr><th scope="col">{t('Показатель')}</th><th scope="col">{t('До')}</th><th scope="col">{t('После')}</th></tr></thead><tbody>{rows(technical)}</tbody></table>
        <p>{t('Батч: {p0} · CPU: {p1} с · пик RAM процесса: {p2} МиБ', { p0: result.batch_size, p1: result.cpu_seconds, p2: Math.round(result.process_peak_rss_bytes / 1024 ** 2) })}</p>
        <p className="baseline-note">{t('Паддинг посчитан для настроенного батча по UTC-дням. Это не оценка ускорения GPU.')}</p>
      </details>
    </>}
    {op.error && <p className="error" role="alert">{t(op.error)}</p>}
    {readError && <p className="error" role="alert">{t(readError)}</p>}
  </section>;
}
