import { useEffect, useRef, useState } from 'react';
import { api } from './api';
import { t, uiLocale } from './i18n';
import { useDialogOperation } from './useDialogOperation';

const fields = [
  ['ann_build_threads', 'Потоков построения ANN', 1, 32],
  ['ann_memory_limit_mib', 'Память ANN (МиБ)', 256, 65536],
  ['ann_partitions', 'Разделов индекса ANN', 1, 4096],
  ['ann_probes', 'Проверяемых разделов ANN', 1, 4096],
  ['ann_rebuild_percent', 'Обновлять при новых векторах (%)', 1, 100],
] as const;
type AnnSettings = { ann_indexing_enabled: boolean } & Record<typeof fields[number][0], number>;
const keys = ['ann_indexing_enabled', ...fields.map(([key]) => key)] as const;
type AnnEntry = {
  space_id: string; roles: string[]; dimension: number; metric?: string; rows: number;
  state: string; ready: boolean; needs_update: boolean; indexed_rows?: number;
  unindexed_rows?: number; error: string | null; elapsed_seconds: number;
};
type AnnStatus = {
  available: boolean; enabled: boolean; initializing: boolean; running: string | null;
  error: string | null; entries: AnnEntry[]; resources: { threads: number; memory_limit_mib: number };
};
const states: Record<string, string> = {
  ready: 'Готов', not_prepared: 'Не подготовлен', small: 'Недостаточно векторов',
  queued: 'В очереди', building: 'Построение ANN', cancelling: 'Останавливаем…',
  cancelled: 'Остановлен', checking: 'Проверяем состояние', failed: 'Ошибка построения ANN',
};
const errors: Record<string, string> = {
  ann_memory_limit: 'Достигнут лимит памяти ANN. Увеличьте его или уменьшите число разделов и повторите построение.',
  ann_query_timeout: 'Построение ANN не завершилось за отведённое время. Повторите попытку.',
  ann_stop_failed: 'Не удалось подтвердить остановку ANN. Приложение повторяет остановку; очистка индекса пока недоступна.',
};

export default function AnnPanel() {
  const [limits, setLimits] = useState<AnnSettings | null>(null);
  const [saved, setSaved] = useState<AnnSettings | null>(null);
  const [status, setStatus] = useState<AnnStatus | null>(null);
  const [settingsError, setSettingsError] = useState('');
  const [statusError, setStatusError] = useState('');
  const [notice, setNotice] = useState('');
  const [action, setAction] = useState<'save' | 'prepare' | 'stop' | null>(null);
  const [reload, setReload] = useState(0);
  const op = useDialogOperation();
  const snapshot = useRef({ limits, saved });
  snapshot.current = { limits, saved };
  useEffect(() => {
    if (op.busy) return;
    let alive = true;
    const current = op.guard();
    const controller = new AbortController();
    api<AnnSettings>('/api/settings', { signal: controller.signal }).then(value => {
      if (!alive || !current()) return;
      const previous = snapshot.current;
      const edits = previous.limits && previous.saved ? Object.fromEntries(keys
        .filter(key => previous.limits![key] !== previous.saved![key])
        .map(key => [key, previous.limits![key]])) : {};
      setLimits({ ...value, ...edits }); setSaved(value); setSettingsError('');
    }).catch(error => { if (alive && current()) setSettingsError(error.message); });
    return () => { alive = false; controller.abort(); };
  }, [op.busy, reload]);
  useEffect(() => {
    if (op.busy) return;
    let alive = true, pending = false;
    const current = op.guard();
    const controller = new AbortController();
    const refresh = async () => {
      if (pending) return;
      pending = true;
      try {
        const value = await api<AnnStatus>('/api/ann-indexes', { signal: controller.signal });
        if (alive && current()) { setStatus(value); setStatusError(''); }
      } catch (error) {
        if (alive && current()) setStatusError(error instanceof Error ? error.message : t('Ошибка соединения.'));
      } finally { pending = false; }
    };
    void refresh();
    const timer = window.setInterval(() => void refresh(), 1000);
    return () => { alive = false; controller.abort(); window.clearInterval(timer); };
  }, [op.busy, reload]);

  const run = (kind: 'save' | 'prepare' | 'stop', task: (current: () => boolean) => Promise<void>) => void op.run(async current => {
    setAction(kind); setNotice('');
    try { await task(current); }
    finally { if (current()) setAction(null); }
  });
  const save = () => run('save', async current => {
    if (!limits || !saved) return;
    const body = Object.fromEntries(keys.filter(key => limits[key] !== saved[key]).map(key => [key, limits[key]]));
    const value = await api<AnnSettings>('/api/settings', { method: 'PATCH', body: JSON.stringify(body) });
    if (current()) { setLimits(value); setSaved(value); setNotice('Настройки ANN сохранены.'); }
  });
  const prepare = (space: string) => run('prepare', async current => {
    const value = await api<AnnStatus>('/api/ann-indexes/prepare', { method: 'POST', body: JSON.stringify({ space_id: space }) });
    if (current()) { setStatus(value); setNotice('Построение ANN поставлено в очередь.'); }
  });
  const stop = () => run('stop', async current => {
    const value = await api<AnnStatus>('/api/ann-indexes/cancel', { method: 'POST' });
    if (current()) {
      setStatus(value);
      setLimits(previous => previous && ({ ...previous, ann_indexing_enabled: false }));
      setSaved(previous => previous && ({ ...previous, ann_indexing_enabled: false }));
      setNotice('Остановка ANN запрошена; автоматическое построение отключено.');
    }
  });
  const valid = limits && fields.every(([key, , min, max]) => Number.isInteger(limits[key]) && limits[key] >= min && limits[key] <= max);
  const changed = limits && saved && keys.some(key => limits[key] !== saved[key]);
  const numbers = (value: number) => value.toLocaleString(uiLocale());
  const hasWork = status && (status.running || status.enabled || status.entries.some(entry => entry.state === 'queued'));
  return <section className="ann-settings" aria-label={t('Ускорение поиска · ANN')}>
    <h3>{t('Ускорение поиска · ANN')}</h3>
    <p className="baseline-note">{t('ANN ускоряет поиск по смыслу. Индексы общие для всех диалогов; векторы моделей сохраняются при перестроении ANN.')}</p>
    {limits && <>
      <label className="check-label"><input type="checkbox" checked={limits.ann_indexing_enabled} disabled={op.busy}
        onChange={event => { setNotice(''); setLimits({ ...limits, ann_indexing_enabled: event.target.checked }); }} />{t('Автоматически готовить и обновлять ANN')}</label>
      <div className="resource-fields">{fields.slice(0, 2).map(([key, label, min, max]) => <label key={key}>{t(label)}
        <input type="number" min={min} max={max} step="1" value={limits[key]} disabled={op.busy}
          onChange={event => { setNotice(''); setLimits({ ...limits, [key]: Number(event.target.value) }); }} /></label>)}</div>
      <details className="ann-advanced"><summary>{t('Расширенные параметры ANN')}</summary>
        <p className="baseline-note">{t('Больше проверяемых разделов повышает полноту ANN и время поиска. Изменение числа разделов требует обновления ANN.')}</p>
        <div className="resource-fields">{fields.slice(2).map(([key, label, min, max]) => <label key={key}>{t(label)}
          <input type="number" min={min} max={max} step="1" value={limits[key]} disabled={op.busy}
            onChange={event => { setNotice(''); setLimits({ ...limits, [key]: Number(event.target.value) }); }} /></label>)}</div>
      </details>
      <p className="baseline-note">{t('Лимиты ANN ограничены общими CPU и RAM. Изменение лимитов перезапускает текущее построение ANN.')}</p>
      <div className="job-actions">
        <button type="button" className="primary" disabled={op.busy || !valid || !changed} onClick={save}>{action === 'save' ? t('Сохраняем…') : t('Сохранить настройки ANN')}</button>
        <button type="button" className="secondary" disabled={op.busy || !hasWork} onClick={stop}>{action === 'stop' ? t('Останавливаем…') : t('Остановить ANN')}</button>
      </div>
    </>}
    {status && <p className="baseline-note">{t('Действующие лимиты: {p0} потоков · {p1} МиБ', { p0: status.resources.threads, p1: status.resources.memory_limit_mib })}</p>}
    {(!status || !limits || status.initializing) && !settingsError && !statusError && <p role="status">{t('Проверяем ANN…')}</p>}
    {status && !status.available && <p className="warning-note">{t('Среда векторного поиска недоступна. Подготовьте текстовую модель или модель изображений.')}</p>}
    {status && !status.initializing && !status.entries.length && <p>{t('Подготовьте текстовую модель или модель изображений, чтобы построить ANN.')}</p>}
    <div className="ann-indexes">{status?.entries.map(entry => {
      const running = ['building', 'cancelling'].includes(entry.state);
      const done = entry.state === 'ready' && !entry.needs_update;
      const title = entry.roles.includes('images') ? 'Изображения' : 'Текст и OCR';
      return <section className="ann-index" key={entry.space_id} aria-label={t('ANN · {p0}', { p0: t(title) })}>
        <div className="model-card-heading"><strong>{t(title)}</strong><span className="model-state">{t(entry.needs_update && entry.state === 'ready' ? 'Требует обновления' : states[entry.state] ?? 'Проверяем состояние')}</span></div>
        <p>{t('Векторов: {p0} · в ANN: {p1}', { p0: numbers(entry.rows), p1: numbers(entry.indexed_rows ?? 0) })}</p>
        {running && <><progress aria-label={t('Построение ANN · {p0}', { p0: t(title) })} /><p className="baseline-note">{t('Прошло {p0} с', { p0: Math.floor(entry.elapsed_seconds) })}</p></>}
        {entry.state === 'small' && <p className="baseline-note">{t('Векторов пока недостаточно для ANN. Доступен полный поиск.')}</p>}
        {entry.error && <p className="error" role="alert">{t(errors[entry.error] ?? 'Не удалось построить ANN. Повторите попытку.')}</p>}
        {!done && entry.state !== 'small' && <button type="button" className="secondary"
          disabled={op.busy || !status.available || status.initializing || !!status.running || entry.state === 'queued' || changed || !valid}
          onClick={() => prepare(entry.space_id)}>{t(entry.state === 'failed' ? 'Повторить построение ANN' : entry.ready ? 'Обновить ANN' : 'Подготовить ANN')}</button>}
        <details className="ann-details"><summary>{t('Подробности индекса ANN')}</summary>
          <code>{entry.space_id}</code><p>{t('Размерность: {p0}', { p0: entry.dimension })} · {entry.metric ?? 'cosine'}</p>
        </details>
      </section>;
    })}</div>
    {changed && <p className="baseline-note">{t('Сохраните параметры ANN перед запуском построения.')}</p>}
    {notice && <p role="status">{t(notice)}</p>}
    {action === 'prepare' && <p role="status">{t('Запрашиваем построение ANN…')}</p>}
    {status?.error && <p className="error" role="alert">{t('Не удалось проверить ANN. Повторите проверку.')}</p>}
    {(settingsError || statusError) && <p className="error" role="alert">{t(settingsError || statusError)}</p>}
    {(settingsError || statusError || status?.error) && <button type="button" disabled={op.busy} onClick={() => setReload(value => value + 1)}>{t('Повторить проверку ANN')}</button>}
    {op.error && <p className="error" role="alert">{t(op.error)}</p>}
  </section>;
}
