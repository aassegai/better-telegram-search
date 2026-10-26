import { useEffect, useState } from 'react';
import { api } from './api';
import { t } from './i18n';
import Dialog from './Dialog';
import { useDialogOperation } from './useDialogOperation';
import SearchHistoryStorage from './SearchHistoryStorage';

type Kind = 'text' | 'images' | 'ocr';
type Sizes = { database_bytes: number; vectors_bytes: number; models_bytes: number; cache_bytes: number; reclaim_estimate_bytes: number; estimate_note: string };
type Inventory = { indexes: Record<Kind, { vectors: number; bytes: number; shared: boolean }> };
type Job = { state: string; kind: Kind | null; error?: string | null; deleted_vectors?: number; freed_bytes?: number };
const kinds = [['text', 'Текстовые векторы'], ['images', 'Векторы изображений'], ['ocr', 'Векторы текста OCR']] as const;
const mib = (value: number) => t('{p0} МиБ', { p0: (value / 1024 ** 2).toFixed(1) });
const active = (job: Job) => job.state === 'waiting' || job.state === 'clearing';

export default function StoragePanel() {
  const [sizes, setSizes] = useState<Sizes | null>(null);
  const [inventory, setInventory] = useState<Inventory | null>(null);
  const [job, setJob] = useState<Job>({ state: 'idle', kind: null });
  const [confirmation, setConfirmation] = useState<Kind | null>(null);
  const [initialized, setInitialized] = useState(false);
  const op = useDialogOperation();
  const working = active(job);
  const refresh = async (current: () => boolean) => {
    const [space, vectors] = await Promise.allSettled([api<Sizes>('/api/storage'), api<Inventory>('/api/storage/vectors')]);
    if (current()) {
      if (space.status === 'fulfilled') setSizes(space.value);
      if (vectors.status === 'fulfilled') setInventory(vectors.value);
    }
    if (space.status === 'rejected') throw space.reason;
    if (vectors.status === 'rejected') throw vectors.reason;
  };
  const reload = async (current: () => boolean) => {
    const value = await api<Job>('/api/storage/vectors/clear');
    if (!current()) return;
    setJob(value);
    if (!active(value)) await refresh(current);
  };
  useEffect(() => {
    let alive = true;
    const valid = op.guard();
    const current = () => alive && valid();
    void reload(current).catch(error => { if (current()) op.setError(error.message); })
      .finally(() => { if (current()) setInitialized(true); });
    return () => { alive = false; };
  }, []);
  useEffect(() => {
    if (!working) return;
    let alive = true;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try {
        const value = await api<Job>('/api/storage/vectors/clear');
        if (!alive) return;
        op.setError('');
        if (!active(value)) {
          try { await refresh(() => alive); }
          catch (error) { if (alive) op.setError(error instanceof Error ? error.message : t('Ошибка операции.')); }
          if (alive) setJob(value);
          return;
        }
        setJob(value);
      } catch (error) { if (alive) op.setError(error instanceof Error ? error.message : t('Ошибка операции.')); }
      if (alive) timer = setTimeout(() => void poll(), 800);
    };
    timer = setTimeout(() => void poll(), 300);
    return () => { alive = false; clearTimeout(timer); };
  }, [working]);
  const clear = () => void op.run(async current => {
    if (!confirmation) return;
    const value = await api<Job>(`/api/storage/vectors/${confirmation}/clear`, { method: 'POST', body: JSON.stringify({ confirm: true }) });
    if (current()) {
      setJob(value); setConfirmation(null);
      if (!active(value)) await refresh(current);
    }
  });
  const busy = op.busy || working || !initialized;
  return <section className="storage-panel" aria-label={t('Место на диске')}>
    <SearchHistoryStorage />
    <h3>{t('Место на диске')}</h3>
    {sizes && <dl className="diagnostics"><dt>{t('База')}</dt><dd>{mib(sizes.database_bytes)}</dd><dt>{t('Векторы')}</dt><dd>{mib(sizes.vectors_bytes)}</dd><dt>{t('Модели и словари')}</dt><dd>{mib(sizes.models_bytes)}</dd><dt>{t('Кэш')}</dt><dd>{mib(sizes.cache_bytes)}</dd><dt>{t('Можно освободить в SQLite')}</dt><dd>{mib(sizes.reclaim_estimate_bytes)}</dd></dl>}
    <p className="baseline-note">{t('Очистка удаляет выбранные векторы во всех диалогах. Сообщения, фотографии, модели и распознанный текст OCR сохраняются. Индекс останется на паузе до ручного запуска.')}</p>
    <div className="vector-storage-list">{kinds.map(([kind, label]) => {
      const entry = inventory?.indexes[kind];
      return <section key={kind} className="vector-storage-card" aria-label={t(label)}>
        <div><strong>{t(label)}</strong>{entry && <small>{t('Векторов: {p0}', { p0: entry.vectors })} · {entry.shared ? '≈ ' : ''}{mib(entry.bytes)}</small>}</div>
        <button className="danger" disabled={busy || !entry || (entry.vectors === 0 && entry.bytes === 0)} onClick={() => setConfirmation(kind)}>{t('Очистить')}</button>
      </section>;
    })}</div>
    {inventory && Object.values(inventory.indexes).some(value => value.shared) && <p className="baseline-note">{t('Для общих таблиц размер каждого типа приблизительный. Очистка сохраняет векторы другого типа и уплотняет файлы.')}</p>}
    {working && <p role="status">{t(job.state === 'waiting' ? 'Ожидаем завершения текущего батча…' : 'Удаляем векторы и освобождаем место…')}</p>}
    {job.state === 'done' && <p role="status">{t('Индекс очищен. Удалено векторов: {p0}. Освобождено: {p1}.', { p0: job.deleted_vectors ?? 0, p1: mib(job.freed_bytes ?? 0) })}</p>}
    {job.error && <p className="error" role="alert">{t(job.error)}</p>}
    <div className="job-actions"><button disabled={busy} onClick={() => void op.run(reload)}>{t('Обновить сведения о месте')}</button>
      <button disabled={busy || !sizes} onClick={() => void op.run(async current => {
        await api<Sizes>('/api/storage/compact', { method: 'POST' }); if (current()) await refresh(current);
      })}>{t('Уплотнить базу и индексы')}</button></div>
    {op.error && <p className="error" role="alert">{t(op.error)}</p>}
    {confirmation && <Dialog titleId="clear-vector-title" onClose={() => { if (!op.busy) setConfirmation(null); }}>
      <h2 id="clear-vector-title">{t('Очистить векторный индекс?')}</h2>
      <p>{t(kinds.find(([kind]) => kind === confirmation)![1])}</p>
      <p>{t('Векторы выбранного типа будут удалены во всех диалогах. Для смыслового поиска их потребуется построить заново. Исходные данные и распознавание OCR сохранятся.')}</p>
      {op.error && <p className="error" role="alert">{t(op.error)}</p>}
      <div className="limit-presets"><button disabled={op.busy} onClick={() => setConfirmation(null)}>{t('Отмена')}</button><button className="danger" disabled={op.busy} onClick={clear}>{op.busy ? t('Запускаем…') : t('Очистить векторы')}</button></div>
    </Dialog>}
  </section>;
}
