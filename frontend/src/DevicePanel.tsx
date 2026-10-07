import { useEffect, useState } from 'react';
import { api } from './api';
import { t } from './i18n';
import { useDialogOperation } from './useDialogOperation';

type DeviceSettings = { device: 'cpu' | 'auto' | 'gpu'; search_device: 'cpu' | 'auto' | 'gpu'; gpu_device_id: number; gpu_memory_limit_mib: number };
type Execution = { provider: string; device: string; warning: string | null };

export default function DevicePanel({ onChange }: { onChange: () => void }) {
  const [settings, setSettings] = useState<DeviceSettings | null>(null);
  const [execution, setExecution] = useState<Execution | null>(null);
  const [queryExecution, setQueryExecution] = useState<Execution | null>(null);
  const [notice, setNotice] = useState('');
  const op = useDialogOperation();
  useEffect(() => {
    let alive = true;
    api<DeviceSettings>('/api/settings').then(value => { if (alive) setSettings(value); })
      .catch(error => { if (alive) setNotice(error instanceof Error ? error.message : t('Не удалось прочитать настройки.')); });
    return () => { alive = false; };
  }, []);
  const valid = settings && Number.isInteger(settings.gpu_device_id ?? 0) &&
    (settings.gpu_device_id ?? 0) >= 0 && (settings.gpu_device_id ?? 0) <= 15 &&
    Number.isInteger(settings.gpu_memory_limit_mib ?? 4096) &&
    (settings.gpu_memory_limit_mib ?? 4096) >= 512 && (settings.gpu_memory_limit_mib ?? 4096) <= 65536;
  const save = () => void op.run(async current => {
    if (!settings) return;
    const result = await api<{ settings: DeviceSettings; execution: Execution; query_execution: Execution }>('/api/device', {
      method: 'POST', body: JSON.stringify({ device: settings.device, gpu_device_id: settings.gpu_device_id,
        gpu_memory_limit_mib: settings.gpu_memory_limit_mib, search_device: settings.search_device ?? 'cpu' }),
    });
    if (current()) { setSettings(result.settings); setExecution(result.execution); setQueryExecution(result.query_execution); setNotice(t('Устройства сохранены. Готовая часть индекса сохранена; индексацию можно продолжить.')); onChange(); }
  });
  return <section className="device-panel">
    <h3>{t('Ускорение поиска')}</h3>
    <p>{t('E5 и CLIP используют ONNX Runtime. NVIDIA требует GPU-сборку Windows или Linux; macOS использует CoreML. Torch не нужен. OCR работает на CPU.')}</p>
    {settings && <>
      <div className="resource-fields">
        <label>{t('Устройство индексации')}<select aria-label={t('Устройство индексации')} disabled={op.busy} value={settings.device}
          onChange={event => setSettings({ ...settings, device: event.target.value as DeviceSettings['device'] })}>
          <option value="cpu">CPU</option><option value="auto">{t('Авто')}</option><option value="gpu">GPU</option>
        </select></label>
        <label>{t('Устройство поисковых запросов')}<select aria-label={t('Устройство поисковых запросов')} disabled={op.busy} value={settings.search_device ?? 'cpu'}
          onChange={event => setSettings({ ...settings, search_device: event.target.value as DeviceSettings['search_device'] })}>
          <option value="cpu">CPU</option><option value="auto">{t('Авто')}</option><option value="gpu">GPU</option>
        </select></label>
        <label>{t('Номер GPU')}<input aria-label={t('Номер GPU')} type="number" min="0" max="15" step="1" disabled={op.busy}
          value={settings.gpu_device_id ?? 0} onChange={event => setSettings({ ...settings, gpu_device_id: Number(event.target.value) })} /></label>
        <label>{t('Память CUDA на модель (МиБ)')}<input aria-label={t('Память CUDA на модель (МиБ)')} type="number" min="512" max="65536" step="1" disabled={op.busy}
          value={settings.gpu_memory_limit_mib ?? 4096} onChange={event => setSettings({ ...settings, gpu_memory_limit_mib: Number(event.target.value) })} /></label>
      </div>
      <p>{t('GPU может строить индекс, а CPU — выполнять запросы в том же индексе. Приостановите текст и медиа перед применением настроек. Готовые чанки и прогресс сохранятся; перестроение при смене устройства не требуется.')}</p>
      <p>{t('После паузы или завершения индексации GPU-сессии выгружаются. При запросах на CPU приложение не загружает GPU-модели. В режиме Авто недоступный GPU заменяется CPU.')}</p>
      <p>{t('Лимит CUDA действует на арену памяти каждой модели; общий расход GPU может быть выше. На CoreML этот лимит не применяется.')}</p>
      <button disabled={op.busy || !valid} onClick={save}>{op.busy ? t('Проверяем устройство…') : t('Применить устройство')}</button>
    </>}
    {execution && <p role="status">{t('Активный runtime: {p0}', { p0: execution.provider })}</p>}
    {queryExecution && <p>{t('Runtime запросов: {p0}', { p0: queryExecution.provider })}</p>}
    {execution?.warning && <p className="warning">{t(execution.warning)}</p>}
    {notice && <p role="status">{t(notice)}</p>}{op.error && <p className="error" role="alert">{t(op.error)}</p>}
  </section>;
}
