import { useEffect, useState } from 'react';
import { api } from './api';
import { t } from './i18n';
import { useDialogOperation } from './useDialogOperation';

const resources = [
  ['cpu_threads', 'Потоков CPU', 1, 32], ['memory_limit_mib', 'Порог RAM для медиа (МиБ)', 1024, 65536],
  ['idle_unload_seconds', 'Выгрузка модели после простоя (сек)', 1, 86400],
  ['query_cache_entries', 'Запросов в кэше', 0, 256], ['retrieval_candidates', 'Кандидатов поиска', 100, 1000],
  ['ocr_timeout_seconds', 'Лимит OCR на фото (сек)', 5, 300], ['ocr_max_edge', 'Максимальная сторона OCR (px)', 512, 4096],
  ['ocr_cpu_workers', 'Воркеров OCR на CPU', 1, 4],
] as const;
type ResourceKey = typeof resources[number][0] | 'gpu_memory_limit_mib' | 'gpu_index_load_percent';
type Limits = Record<ResourceKey, number>;
const fields = [...resources, ['gpu_memory_limit_mib', '', 512, 65536], ['gpu_index_load_percent', '', 10, 100]] as const;
const presets = [['Экономный', 25], ['Сбалансированный', 60], ['Максимальный', 100]] as const;

export default function ResourcePanel() {
  const [limits, setLimits] = useState<Limits | null>(null);
  const [saved, setSaved] = useState<Limits | null>(null);
  const [notice, setNotice] = useState('');
  const op = useDialogOperation();
  useEffect(() => {
    let alive = true;
    api<Limits>('/api/settings').then(value => {
      if (alive) { setLimits(value); setSaved(value); }
    }).catch(error => { if (alive) op.setError(error.message); });
    return () => { alive = false; };
  }, []);
  const save = () => void op.run(async current => {
    if (!limits || !saved) return;
    setNotice('');
    // Send only edited fields so a live GPU load adjustment does not acquire
    // the lifecycle lock required by CPU, memory and OCR resource changes.
    const body = Object.fromEntries(fields.filter(([key]) => limits[key] !== saved[key])
      .map(([key]) => [key, limits[key]]));
    const value = await api<Limits>('/api/settings', { method: 'PATCH', body: JSON.stringify(body) });
    if (current()) { setLimits(value); setSaved(value); setNotice('Настройки ресурсов сохранены.'); }
  });
  const valid = limits && fields.every(([key, , min, max]) => Number.isInteger(limits[key])
    && limits[key] >= min && limits[key] <= max);
  const changed = limits && saved && fields.some(([key]) => limits[key] !== saved[key]);
  return <details className="resource-settings index-advanced">
    <summary>{t('Общие ресурсы индексации')}</summary>
    <p className="baseline-note">{t('Общие ограничения ресурсов применяются ко всем диалогам.')}</p>
    <section aria-label={t('CPU, память и очереди')}>
      <h3>{t('CPU, память и очереди')}</h3>
      <p>{t('Перед сохранением приостановите индексацию текста и медиа. Порог RAM проверяется перед фоновой обработкой фотографии; он не ограничивает память процесса жёстко.')}</p>
      {limits && <div className="resource-fields">{resources.map(([key, label, min, max]) => <label key={key}>{t(label)}<input
        aria-label={t(label)} type="number" min={min} max={max} step="1" value={limits[key]} disabled={op.busy}
        onChange={event => setLimits({ ...limits, [key]: Number(event.target.value) })} /></label>)}</div>}
    </section>
    <section className="gpu-performance" aria-label={t('Лимиты GPU')}>
      <h3>{t('Производительность GPU')}</h3>
      {limits && <>
        <label>{t('Видеопамять на модель (МиБ)')}<input type="number" min="512" max="65536" step="1"
          aria-label={t('Видеопамять на модель (МиБ)')} value={limits.gpu_memory_limit_mib} disabled={op.busy}
          onChange={event => setLimits({ ...limits, gpu_memory_limit_mib: Number(event.target.value) })} /></label>
        <div className="limit-presets">{[2048, 4096, 6144, 8192].map(value => <button key={value} type="button" disabled={op.busy}
          aria-pressed={limits.gpu_memory_limit_mib === value} onClick={() => setLimits({ ...limits, gpu_memory_limit_mib: value })}>{value} {t('МиБ')}</button>)}</div>
        <p className="baseline-note">{t('Лимит памяти действует на каждую CUDA-модель; общий расход может быть выше. CoreML этот лимит не использует. Для изменения памяти приостановите индексацию.')}</p>
        <label>{t('Нагрузка индексации: {p0}%', { p0: limits.gpu_index_load_percent })}<input type="range" min="10" max="100" step="5"
          aria-label={t('Нагрузка индексации GPU')} value={limits.gpu_index_load_percent} disabled={op.busy}
          onChange={event => setLimits({ ...limits, gpu_index_load_percent: Number(event.target.value) })} /></label>
        <div className="limit-presets">{presets.map(([label, value]) => <button key={value} type="button" disabled={op.busy}
          aria-pressed={limits.gpu_index_load_percent === value} onClick={() => setLimits({ ...limits, gpu_index_load_percent: value })}>{t(label)}</button>)}</div>
        <p className="baseline-note">{t('Нагрузка регулируется паузами между GPU-батчами и применяется сразу. Это приблизительный лимит; поиск выполняется без искусственных пауз.')}</p>
      </>}
    </section>
    <button className="primary" disabled={op.busy || !valid || !changed} onClick={save}>{op.busy ? t('Сохраняем…') : t('Сохранить ресурсы')}</button>
    {notice && <p role="status">{t(notice)}</p>}{op.error && <p className="error" role="alert">{t(op.error)}</p>}
  </details>;
}
