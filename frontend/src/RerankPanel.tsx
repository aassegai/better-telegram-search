import { useEffect, useState } from 'react';
import { api } from './api';
import { t } from './i18n';
import { useDialogOperation } from './useDialogOperation';
import ModelCard from './ModelCard';
import { ModelDeviceSummary } from './ModelSettings';

type Status = { ready?: boolean; enabled: boolean; preparation_state: string; error: string | null;
  path: string; device: string; download_bytes: number; download_completed_bytes: number };

export default function RerankPanel({ onPerformance }: { onPerformance: () => void }) {
  const [status, setStatus] = useState<Status | null>(null);
  const [enabled, setEnabled] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [offline, setOffline] = useState(false);
  const [loadError, setLoadError] = useState('');
  const op = useDialogOperation();
  useEffect(() => {
    if (op.busy) return;
    let alive = true;
    let pending = false;
    const controller = new AbortController();
    const read = () => {
      if (pending) return; pending = true;
      void api<Status>('/api/rerank', { signal: controller.signal }).then(value => {
        if (alive) { setStatus(value); setLoadError(''); if (!dirty) setEnabled(value.enabled); }
      }).catch(error => { if (alive) setLoadError(error.message); }).finally(() => { pending = false; });
    };
    read(); const timer = window.setInterval(read, 2000);
    return () => { alive = false; controller.abort(); window.clearInterval(timer); };
  }, [op.busy, dirty]);
  const preparing = ['downloading', 'preparing'].includes(status?.preparation_state ?? '');
  const ready = Boolean(status?.ready ?? status?.preparation_state === 'ready');
  return <ModelCard title="Уточнение результатов · Giga" purpose="Дополнительно сортирует до 50 текстовых и до 50 OCR-фрагментов. Индекс не перестраивается. Поиск может занять больше времени."
    ready={ready} preparing={preparing} error={op.error || loadError || status?.error}
    details={<div className="model-paths"><span>{t('Папка загрузки модели')}</span><code>{status?.path}</code></div>}
    device={<ModelDeviceSummary role="e5" queryOnly onPerformance={onPerformance} />}
    download={status ? { completed: status.download_completed_bytes, total: status.download_bytes } : undefined}
    options={<>
      <label className="check-label"><input type="checkbox" aria-label={t('Переранжирование Giga')} checked={enabled}
        disabled={!status || op.busy || (!ready && !enabled)} onChange={event => { setEnabled(event.target.checked); setDirty(true); }} />{t('Переранжирование Giga')}</label>
      <label className="check-label"><input type="checkbox" checked={offline} disabled={op.busy || preparing} onChange={event => setOffline(event.target.checked)} />{t('Только локальный кэш моделей')}</label>
    </>}
    actions={<>
      <button className="secondary" disabled={!dirty || op.busy || !status} onClick={() => void op.run(async current => {
        await api('/api/settings', { method: 'PATCH', body: JSON.stringify({ giga_rerank_enabled: enabled }) });
        const value = await api<Status>('/api/rerank'); if (current()) { setStatus(value); setEnabled(value.enabled); setDirty(false); }
      })}>{t('Сохранить настройки модели')}</button>
      <button className="primary" disabled={!status || op.busy || preparing || ready} onClick={() => void op.run(async current => {
        const value = await api<Status>('/api/rerank/prepare', { method: 'POST', body: JSON.stringify({ offline }) });
        if (current()) setStatus(value);
      })}>{t('Подготовить Giga')}</button>
    </>}>
    <p>Giga · {t('Встроенная загрузка')}</p>
    {status && <small>{t('Размер загрузки: {p0} МиБ', { p0: (status.download_bytes / 1024 ** 2).toFixed(0) })}</small>}
  </ModelCard>;
}
