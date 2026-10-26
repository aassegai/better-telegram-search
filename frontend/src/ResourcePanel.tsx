import { useEffect, useRef, useState } from 'react';
import { api } from './api';
import { t } from './i18n';
import { useDialogOperation } from './useDialogOperation';
import BatchFields, { batchFields } from './BatchFields';
import type { BatchKey, Batches } from './BatchFields';
import { useModelSettings } from './ModelSettings';

const resources = [
  ['cpu_threads', 'Потоков CPU', 1, 32], ['memory_limit_mib', 'Порог RAM для медиа (МиБ)', 1024, 65536],
  ['idle_unload_seconds', 'Выгрузка модели после простоя (сек)', 1, 86400],
  ['query_cache_entries', 'Запросов в кэше', 0, 256], ['retrieval_candidates', 'Кандидатов поиска', 100, 1000],
  ['ocr_timeout_seconds', 'Лимит OCR на фото (сек)', 5, 300], ['ocr_max_edge', 'Максимальная сторона OCR (px)', 512, 4096],
  ['ocr_cpu_workers', 'Воркеров OCR на CPU', 1, 4],
] as const;
type ResourceKey = typeof resources[number][0] | 'gpu_memory_limit_mib' | 'gpu_index_load_percent' | BatchKey;
type Limits = Record<ResourceKey, number>;
const fields = [...resources, ['gpu_memory_limit_mib', '', 512, 65536], ['gpu_index_load_percent', '', 10, 100],
  ...batchFields.map(([key, label, max]) => [key, label, key === 'ocr_region_batch_size' ? 0 : 1, max] as const)] as const;
const presets = [['Экономный', 25], ['Сбалансированный', 60], ['Максимальный', 100]] as const;

export default function ResourcePanel() {
  const [limits, setLimits] = useState<Limits | null>(null);
  const [saved, setSaved] = useState<Limits | null>(null);
  const [notice, setNotice] = useState('');
  const op = useDialogOperation();
  const { models } = useModelSettings();
  const snapshot = useRef({ limits, saved });
  snapshot.current = { limits, saved };
  useEffect(() => {
    if (op.busy) return;
    let alive = true;
    const current = op.guard();
    api<Limits>('/api/settings').then(value => {
      if (alive && current()) {
        const previous = snapshot.current;
        // Choosing a model/device can normalize related global settings (for
        // example Paddle GPU forces one CPU worker). Refresh saved values while
        // preserving fields the user has actually edited in this form.
        const edits = previous.limits && previous.saved ? Object.fromEntries(fields
          .filter(([key]) => previous.limits![key] !== previous.saved![key])
          .map(([key]) => [key, previous.limits![key]])) : {};
        setLimits({ ...value, ...edits }); setSaved(value);
      }
    }).catch(error => { if (alive && current()) op.setError(error.message); });
    return () => { alive = false; };
  }, [models, op.busy]);
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
  return <section className="resource-settings" aria-label={t('Ресурсы приложения')}>
    <h3>{t('Ресурсы приложения')}</h3>
    <p className="baseline-note">{t('Общие ограничения ресурсов применяются ко всем диалогам.')}</p>
    <section aria-label={t('CPU, память и очереди')}>
      <h3>{t('CPU, память и очереди')}</h3>
      <p>{t('Перед сохранением приостановите индексацию текста и медиа. Порог RAM проверяется перед фоновой обработкой фотографии; он не ограничивает память процесса жёстко.')}</p>
      <p className="baseline-note">{t('Порог RAM учитывает всё приложение и дочерние OCR-процессы. Даже при свободной памяти системы превышение этого порога приостанавливает медиаиндексацию.')}</p>
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
    <section aria-label={t('Батчи индексации')}><h3>{t('Батчи индексации')}</h3>
      <p className="baseline-note">{t('Общие значения используются в диалогах без собственного переопределения. Для областей OCR значение 0 означает автоматический выбор.')}</p>
      {limits && <BatchFields values={limits} defaults={limits as Batches} disabled={op.busy}
        onChange={(key, value) => { setNotice(''); setLimits({ ...limits, [key]: value! }); }} />}
    </section>
    <button className="primary" disabled={op.busy || !valid || !changed} onClick={save}>{op.busy ? t('Сохраняем…') : t('Сохранить ресурсы')}</button>
    {notice && <p role="status">{t(notice)}</p>}{op.error && <p className="error" role="alert">{t(op.error)}</p>}
  </section>;
}
