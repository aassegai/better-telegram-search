import { t } from './i18n';
import { useEffect, useState } from 'react';
import { api } from './api';
import type { MediaStatus } from './types';
import { useDialogOperation } from './useDialogOperation';
import IndexCard from './IndexCard';
import OcrIndexCard from './OcrIndexCard';
import type { Mutation } from './IndexCard';
import DevicePanel from './DevicePanel';

type Sizes = { database_bytes: number; vectors_bytes: number; models_bytes: number; cache_bytes: number; reclaim_estimate_bytes: number; estimate_note: string };
const resources = [
  ['cpu_threads', 'Потоков CPU', 1, 32], ['memory_limit_mib', 'Порог RAM для медиа (МиБ)', 1024, 65536],
  ['idle_unload_seconds', 'Выгрузка модели после простоя (сек)', 1, 86400],
  ['query_cache_entries', 'Запросов в кэше', 0, 256], ['retrieval_candidates', 'Кандидатов поиска', 100, 1000],
  ['ocr_timeout_seconds', 'Лимит OCR на фото (сек)', 5, 300], ['ocr_max_edge', 'Максимальная сторона OCR (px)', 512, 4096],
] as const;
const mib = (value: number) => t("{p0} МиБ", { p0: (value / 1024 ** 2).toFixed(1) });

export default function WorkspacePanel({ media, onMediaChange, chatId, indexing = false, onStart, onEnd, pending, onModels }: Mutation & { media: MediaStatus | null; onMediaChange: (value: MediaStatus) => void; chatId?: string; indexing?: boolean; onModels?: () => void }) {
  const [settings, setSettings] = useState<Record<string, number> | null>(null);
  const [sizes, setSizes] = useState<Sizes | null>(null);
  const [offline, setOffline] = useState(false);
  const [ocrOffline, setOcrOffline] = useState(false);
  const [notice, setNotice] = useState('');
  const op = useDialogOperation();
  const busy = op.busy || Boolean(pending);
  useEffect(() => {
    let alive = true;
    Promise.all([api<Record<string, number>>('/api/settings'), api<Sizes>('/api/storage')])
      .then(([settings, sizes]) => { if (alive) { setSettings(settings); setSizes(sizes); } })
      .catch(error => { if (alive) setNotice(error instanceof Error ? error.message : t('Не удалось прочитать настройки.')); });
    return () => { alive = false; };
  }, []);
  const accept = (value: unknown) => { const result = value as MediaStatus | { media: MediaStatus }; onMediaChange('media' in result ? result.media : result); };
  const prepare = (kind: 'ocr' | 'images') => void op.run(async current => {
    onStart?.();
    try {
      const value = await api(chatId ? `/api/chats/${encodeURIComponent(chatId)}/index/media/prepare` : '/api/media-index/prepare', { method: 'POST', body: JSON.stringify({ kind, offline: kind === 'ocr' ? ocrOffline : offline }) });
      if (current()) accept(value);
    } finally { onEnd?.(); }
  });
  const control = (action: string, kind: 'images' | 'ocr' = 'images') => void op.run(async current => {
    onStart?.();
    try {
      const value = await api(chatId ? `/api/chats/${encodeURIComponent(chatId)}/index/${kind}/${action}` : `/api/${kind === 'ocr' ? 'ocr-index' : 'image-index'}/${action}`, { method: 'POST' });
      if (current()) accept(value);
    } finally { onEnd?.(); }
  });
  const save = () => void op.run(async current => {
    if (!settings) return;
    const body = Object.fromEntries(resources.map(([key]) => [key, settings[key]]));
    const value = await api<Record<string, number>>('/api/settings', { method: 'PATCH', body: JSON.stringify(body) });
    if (current()) { setSettings(value); setNotice(t('Настройки сохранены. Изменение размера OCR обновляет версию кэша.')); }
  });
  const preparing = media?.preparation_state === 'preparing';
  const refreshDevice = async (current: () => boolean) => {
    const value = await api<MediaStatus>('/api/media-index'); if (current()) onMediaChange(value);
  };
  return <section className="workspace-panel">
    {indexing && <>
      {media && <IndexCard title={t('Изображения')} model="CLIP" ready={media.images_ready} total={media.total_photos}
        paused={Boolean(media.paused)} enabled={media.images_enabled === 1} preparing={preparing}
        seconds={media.images_estimated_remaining_seconds} batch={media.batch_size ?? 1} kind="image_batch"
        chatId={chatId} busy={busy} onPrepare={onModels ?? (() => {})} onControl={control}
        onSaved={accept} onStart={onStart} onEnd={onEnd}>
        {[media.error, media.resource_error].filter(Boolean).map(value => <p className="warning" key={value}>{t(value)}</p>)}
        {media.missing_refs > 0 && <p className="warning">{t('Недоступных фотографий в источниках: ')}{media.missing_refs}</p>}
        {(media.images_failed ?? 0) > 0 && <p className="warning">{t('Фотографий с ошибкой: {p0}', { p0: media.images_failed ?? 0 })}</p>}
      </IndexCard>}
      {media && <OcrIndexCard media={media} busy={busy} preparing={preparing} onModels={onModels ?? (() => {})} onControl={action => control(action, 'ocr')} />}
      <p className="baseline-note">{t('Подготовка моделей и выбор устройств находятся в общих настройках.')}</p>
    </>}
    {!indexing && <>
    <section className="model-setup"><h3>{t('Модель изображений · CLIP')}</h3>
      <DevicePanel model="clip" onChange={refreshDevice} />
      <label><input type="checkbox" checked={offline} disabled={busy || preparing} onChange={event => setOffline(event.target.checked)} />{t('Только локальный кэш моделей')}</label>
      <button disabled={busy || preparing} onClick={() => prepare('images')}>{t('Подготовить CLIP')}</button>
      {preparing && <p role="status">{t('Подготавливаем…')}</p>}
      {media?.error && <p className="warning" role="alert">{t(media.error)}</p>}
    </section>
    <section className="model-setup"><h3>{t('Модель распознавания · OCR')}</h3>
      <DevicePanel model="ocr" onChange={refreshDevice} />
      <label><input type="checkbox" checked={ocrOffline} disabled={busy || preparing} onChange={event => setOcrOffline(event.target.checked)} />{t('Только локальный кэш моделей')}</label>
      <button disabled={busy || preparing || !media?.ocr_runtime_installed} onClick={() => prepare('ocr')}>{t('Подготовить OCR')}</button>
    </section>
      <details className="index-advanced"><summary>{t('Общие ресурсы индексации')}</summary>
    <h3>{t("Общие ресурсы индексации")}</h3>
    <p className="baseline-note">{t('Общие ограничения ресурсов применяются ко всем диалогам.')}</p>
    <p>{t("Перед сохранением приостановите индексацию текста и медиа. Порог RAM проверяется перед фоновой обработкой фотографии; он не ограничивает память процесса жёстко.")}</p>
    {settings && <div className="resource-fields">{resources.map(([key, label, min, max]) => <label key={key}>{t(label)}<input aria-label={t(label)} type="number" min={min} max={max} step="1" value={settings[key]} disabled={busy} onChange={event => setSettings({ ...settings, [key]: Number(event.target.value) })} /></label>)}</div>}
    <button disabled={!settings || op.busy || preparing} onClick={save}>{t("Сохранить ресурсы")}</button>
    </details>
    <h3>{t("Место на диске")}</h3>
    {sizes && <><dl className="diagnostics"><dt>{t("База")}</dt><dd>{mib(sizes.database_bytes)}</dd><dt>{t("Векторы")}</dt><dd>{mib(sizes.vectors_bytes)}</dd><dt>{t("Модели и словари")}</dt><dd>{mib(sizes.models_bytes)}</dd><dt>{t("Кэш")}</dt><dd>{mib(sizes.cache_bytes)}</dd><dt>{t("Можно освободить в SQLite")}</dt><dd>{mib(sizes.reclaim_estimate_bytes)}</dd></dl><p>{t(sizes.estimate_note)}</p></>}
    <div className="job-actions"><button disabled={busy || !sizes} onClick={() => void op.run(async current => { const value = await api<Sizes>('/api/storage'); if (current()) setSizes(value); })}>{t("Обновить сведения о месте")}</button>
    <button disabled={busy || !sizes} onClick={() => void op.run(async current => { const value = await api<Sizes>('/api/storage/compact', { method: 'POST' }); if (current()) setSizes(value); })}>{t("Уплотнить базу и индексы")}</button></div>
    </>}
    {notice && <p role="status">{t(notice)}</p>}{op.error && <p className="error" role="alert">{t(op.error)}</p>}
  </section>;
}
