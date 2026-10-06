import { useEffect, useState } from 'react';
import { api } from './api';
import type { MediaStatus } from './types';
import { useDialogOperation } from './useDialogOperation';

type Source = { id: number; chat_name: string; path: string; available: boolean; media_refs: number; ready_refs: number };
type Sizes = { database_bytes: number; vectors_bytes: number; models_bytes: number; cache_bytes: number; reclaim_estimate_bytes: number; estimate_note: string };
const resources = [
  ['cpu_threads', 'Потоков CPU', 1, 32], ['embedding_batch', 'Батч текста', 1, 32],
  ['image_batch', 'Батч изображений', 1, 8], ['memory_limit_mib', 'Порог RAM для медиа (МиБ)', 1024, 65536],
  ['idle_unload_seconds', 'Выгрузка модели после простоя (сек)', 1, 86400],
  ['query_cache_entries', 'Запросов в кэше', 0, 256], ['retrieval_candidates', 'Кандидатов поиска', 100, 1000],
  ['ocr_timeout_seconds', 'Лимит OCR на фото (сек)', 5, 300], ['ocr_max_edge', 'Максимальная сторона OCR (px)', 512, 4096],
] as const;
const mib = (value: number) => `${(value / 1024 ** 2).toFixed(1)} МиБ`;

export default function WorkspacePanel({ media, onMediaChange }: { media: MediaStatus | null; onMediaChange: (value: MediaStatus) => void }) {
  const [settings, setSettings] = useState<Record<string, number> | null>(null);
  const [sources, setSources] = useState<Source[]>([]);
  const [paths, setPaths] = useState<Record<number, string>>({});
  const [sizes, setSizes] = useState<Sizes | null>(null);
  const [offline, setOffline] = useState(false);
  const [notice, setNotice] = useState('');
  const op = useDialogOperation();
  useEffect(() => {
    let alive = true;
    Promise.all([api<Record<string, number>>('/api/settings'), api<Source[]>('/api/sources'), api<Sizes>('/api/storage')])
      .then(([settings, sources, sizes]) => { if (alive) { setSettings(settings); setSources(sources); setSizes(sizes); } })
      .catch(error => { if (alive) setNotice(error instanceof Error ? error.message : 'Не удалось прочитать настройки.'); });
    return () => { alive = false; };
  }, []);
  const prepare = (kind: 'ocr' | 'images') => void op.run(async current => {
    const value = await api<MediaStatus>('/api/media-index/prepare', { method: 'POST', body: JSON.stringify({ kind, offline }) });
    if (current()) onMediaChange(value);
  });
  const control = (action: string) => void op.run(async current => {
    const value = await api<MediaStatus>(`/api/media-index/${action}`, { method: 'POST' });
    if (current()) onMediaChange(value);
  });
  const save = () => void op.run(async current => {
    if (!settings) return;
    const body = Object.fromEntries(resources.map(([key]) => [key, settings[key]]));
    const value = await api<Record<string, number>>('/api/settings', { method: 'PATCH', body: JSON.stringify(body) });
    if (current()) { setSettings(value); setNotice('Настройки сохранены. Изменение размера OCR обновляет версию кэша.'); }
  });
  const checkSource = (source: Source, relink: boolean) => void op.run(async current => {
    const result = await api<{ counts: Record<string, number> }>(`/api/sources/${source.id}/${relink ? 'relink' : 'check'}`, {
      method: 'POST', ...(relink ? { body: JSON.stringify({ path: paths[source.id] || source.path, expected_path: source.path }) } : {}),
    });
    const values = await api<Source[]>('/api/sources');
    if (current()) { setSources(values); setNotice(`Проверено: ${result.counts.ready} доступно, ${result.counts.missing} отсутствует, ${result.counts.changed} изменилось. Изменённые и непроверенные файлы требуют повторного импорта.`); }
  });
  const preparing = media?.preparation_state === 'preparing';
  return <section className="workspace-panel">
    <h3>Фотографии и OCR</h3>
    <p>OCR распознаёт русский и английский текст. CLIP ищет фотографии по описанию. Обработка проходит локально; подготовка скачивает закреплённые модели и словари.</p>
    {media && <>
      <p role="status">OCR: {media.ocr_ready} / {media.total_photos} · OCR по смыслу: {media.ocr_dense_ready} · изображения: {media.images_ready} / {media.total_photos}{media.paused ? ' · на паузе' : ''}</p>
      {media.ocr_failed > 0 && <p className="warning">OCR с ошибкой: {media.ocr_failed}</p>}
      {media.missing_refs > 0 && <p className="warning">Недоступных фотографий в источниках: {media.missing_refs}</p>}
      {preparing && <p role="status">Подготавливаются модели медиа…</p>}
      {[media.error, media.resource_error].filter(Boolean).map(value => <p className="warning" key={value}>{value}</p>)}
      {!media.ocr_runtime_installed && <p>OCR runtime не установлен. Для этой платформы подготовьте профиль OCR перед включением.</p>}
      <label><input type="checkbox" checked={offline} disabled={op.busy || preparing} onChange={event => setOffline(event.target.checked)} />Только локальный кэш моделей</label>
      <div className="job-actions">
        <button disabled={op.busy || preparing || !media.ocr_runtime_installed} onClick={() => prepare('ocr')}>Подготовить OCR</button>
        <button disabled={op.busy || preparing} onClick={() => prepare('images')}>Подготовить поиск фотографий</button>
        <button disabled={op.busy || preparing} onClick={() => control(media.paused ? 'resume' : 'pause')}>{media.paused ? 'Продолжить медиа' : 'Пауза медиа'}</button>
        <button disabled={op.busy || preparing} onClick={() => control('retry')}>Повторить ошибки медиа</button>
      </div>
    </>}
    <h3>Ресурсы</h3>
    <p>Перед сохранением приостановите индексацию текста и медиа. Порог RAM проверяется перед фоновой обработкой фотографии; он не ограничивает память процесса жёстко.</p>
    {settings && <div className="resource-fields">{resources.map(([key, label, min, max]) => <label key={key}>{label}<input aria-label={label} type="number" min={min} max={max} step="1" value={settings[key]} disabled={op.busy} onChange={event => setSettings({ ...settings, [key]: Number(event.target.value) })} /></label>)}</div>}
    <button disabled={!settings || op.busy || preparing} onClick={save}>Сохранить ресурсы</button>
    <h3>Источники</h3>
    {sources.map(source => <div className="source-row" key={source.id}><strong>{source.chat_name}</strong><p>{source.available ? 'Папка доступна' : 'Папка отсутствует'} · вложений: {source.ready_refs || 0} / {source.media_refs}</p>
      <label>Папка источника<input aria-label={`Папка источника ${source.chat_name}`} value={paths[source.id] ?? source.path} disabled={op.busy} onChange={event => setPaths({ ...paths, [source.id]: event.target.value })} /></label>
      <div className="job-actions"><button disabled={op.busy} onClick={() => checkSource(source, false)}>Проверить файлы</button><button disabled={op.busy || !paths[source.id] || paths[source.id] === source.path} onClick={() => checkSource(source, true)}>Привязать новую папку</button></div>
    </div>)}
    <h3>Место на диске</h3>
    {sizes && <><dl className="diagnostics"><dt>База</dt><dd>{mib(sizes.database_bytes)}</dd><dt>Векторы</dt><dd>{mib(sizes.vectors_bytes)}</dd><dt>Модели и словари</dt><dd>{mib(sizes.models_bytes)}</dd><dt>Кэш</dt><dd>{mib(sizes.cache_bytes)}</dd><dt>Можно освободить в SQLite</dt><dd>{mib(sizes.reclaim_estimate_bytes)}</dd></dl><p>{sizes.estimate_note}</p></>}
    <div className="job-actions"><button disabled={op.busy || !sizes} onClick={() => void op.run(async current => { const value = await api<Sizes>('/api/storage'); if (current()) setSizes(value); })}>Обновить сведения о месте</button>
    <button disabled={op.busy || !sizes} onClick={() => void op.run(async current => { const value = await api<Sizes>('/api/storage/compact', { method: 'POST' }); if (current()) setSizes(value); })}>Уплотнить базу и индексы</button></div>
    {notice && <p role="status">{notice}</p>}{op.error && <p className="error" role="alert">{op.error}</p>}
  </section>;
}
