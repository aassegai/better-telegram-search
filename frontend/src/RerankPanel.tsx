import { useEffect, useState } from 'react';
import { api } from './api';
import { t } from './i18n';
import { useDialogOperation } from './useDialogOperation';

type Status = { ready?: boolean; enabled: boolean; preparation_state: string; error: string | null;
  path: string; device: string; download_bytes: number; download_completed_bytes: number };

export default function RerankPanel() {
  const [status, setStatus] = useState<Status | null>(null);
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
      void api<Status>('/api/rerank', { signal: controller.signal }).then(value => { if (alive) { setStatus(value); setLoadError(''); } })
      .catch(error => { if (alive) setLoadError(error instanceof Error ? error.message : t('Не удалось прочитать настройки.')); }).finally(() => { pending = false; });
    };
    void read(); const timer = window.setInterval(() => { void read(); }, 2000);
    return () => { alive = false; controller.abort(); window.clearInterval(timer); };
  }, [op.busy]);
  const preparing = ['downloading', 'preparing'].includes(status?.preparation_state ?? '');
  return <section className="model-setup">
    <h3>{t('Уточнение результатов · Giga')}</h3>
    <p className="baseline-note">{t('Дополнительно сортирует до 50 текстовых и до 50 OCR-фрагментов. Индекс не перестраивается. Поиск может занять больше времени.')}</p>
    {status && <>
      <label><input type="checkbox" aria-label={t('Переранжирование Giga')} checked={status.enabled}
        disabled={op.busy || (!(status.ready ?? status.preparation_state === 'ready') && !status.enabled)}
        onChange={event => { const enabled = event.target.checked; const previous = status; setStatus({ ...status, enabled }); void op.run(async current => {
          try {
          await api('/api/settings', { method: 'PATCH', body: JSON.stringify({ giga_rerank_enabled: enabled }) });
          const value = await api<Status>('/api/rerank'); if (current()) setStatus(value);
          } catch (error) { if (current()) setStatus(previous); throw error; }
        }); }} />{t('Переранжирование Giga')}</label>
      <p>{t('Устройство: {p0} — как у текстового поиска', { p0: status.device.toUpperCase() })}</p>
      <div className="model-paths"><span>{t('Папка загрузки модели')}</span><code>{status.path}</code></div>
      <small>{t('Размер загрузки: {p0} МиБ', { p0: (status.download_bytes / 1024 ** 2).toFixed(0) })}</small>
      <label><input type="checkbox" checked={offline} disabled={op.busy || preparing}
        onChange={event => setOffline(event.target.checked)} />{t('Только локальный кэш моделей')}</label>
      <button disabled={op.busy || preparing} onClick={() => void op.run(async current => {
        const value = await api<Status>('/api/rerank/prepare', { method: 'POST', body: JSON.stringify({ offline }) });
        if (current()) setStatus(value);
      })}>{t('Подготовить Giga')}</button>
      {preparing && <div className="model-download"><progress aria-label={t('Загрузка Giga')}
        value={status.download_completed_bytes} max={Math.max(1, status.download_bytes)} /><small>{t('Подготавливаем…')}</small></div>}
      {(status.ready ?? status.preparation_state === 'ready') && <p role="status">{t('Giga готова')}</p>}
      {status.error && <p className="warning" role="alert">{t(status.error)}</p>}
    </>}
    {(op.error || loadError) && <p className="error" role="alert">{t(op.error || loadError)}</p>}
  </section>;
}
