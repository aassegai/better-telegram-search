import { useEffect, useState } from 'react';
import { api } from './api';
import { t } from './i18n';
import { useDialogOperation } from './useDialogOperation';

type Limits = { gpu_memory_limit_mib: number; gpu_index_load_percent: number };
const presets = [['Экономный', 25], ['Сбалансированный', 60], ['Максимальный', 100]] as const;

export default function GpuPerformancePanel() {
  const [limits, setLimits] = useState<Limits | null>(null);
  const [savedMemory, setSavedMemory] = useState(4096);
  const [notice, setNotice] = useState('');
  const op = useDialogOperation();
  useEffect(() => {
    let alive = true;
    api<Limits>('/api/settings').then(value => {
      if (alive) { setLimits(value); setSavedMemory(value.gpu_memory_limit_mib); }
    }).catch(error => { if (alive) op.setError(error.message); });
    return () => { alive = false; };
  }, []);
  const save = () => void op.run(async current => {
    if (!limits) return;
    const body = { gpu_index_load_percent: limits.gpu_index_load_percent,
      ...(limits.gpu_memory_limit_mib !== savedMemory ? { gpu_memory_limit_mib: limits.gpu_memory_limit_mib } : {}) };
    const value = await api<Limits>('/api/settings', { method: 'PATCH', body: JSON.stringify(body) });
    if (current()) { setLimits(value); setSavedMemory(value.gpu_memory_limit_mib); setNotice('Лимиты GPU сохранены.'); }
  });
  const valid = limits && Number.isInteger(limits.gpu_memory_limit_mib)
    && limits.gpu_memory_limit_mib >= 512 && limits.gpu_memory_limit_mib <= 65536;
  return <details className="gpu-performance index-advanced">
    <summary>{t('Производительность GPU')}</summary>
    <section aria-label={t('Лимиты GPU')}>
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
        <button className="primary" disabled={op.busy || !valid} onClick={save}>{op.busy ? t('Сохраняем…') : t('Сохранить лимиты GPU')}</button>
      </>}
      {notice && <p role="status">{t(notice)}</p>}{op.error && <p className="error" role="alert">{t(op.error)}</p>}
    </section>
  </details>;
}
