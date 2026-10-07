import { useEffect, useState } from 'react';
import { api } from './api';
import { t } from './i18n';
import { useDialogOperation } from './useDialogOperation';

type DeviceSettings = { device: 'cpu' | 'auto' | 'gpu'; search_device: 'cpu' | 'auto' | 'gpu'; gpu_device_id: number; gpu_memory_limit_mib: number };
type Execution = { provider: string; device: string; warning: string | null };

export default function DevicePanel({ onChange }: { onChange: () => void }) {
  const [settings, setSettings] = useState<DeviceSettings | null>(null);
  const [execution, setExecution] = useState<Execution | null>(null);
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
    if (current()) { setSettings(result.settings); setExecution(result.execution); setNotice(t('Устройства сохранены.')); onChange(); }
  });
  return <section className="device-panel">
    <h3>{t('Ускорение поиска')}</h3>
    {settings && <>
      <div className="resource-fields">
        <label>{t('Устройство для индексации')}<select aria-label={t('Устройство для индексации')} disabled={op.busy} value={settings.device}
          onChange={event => setSettings({ ...settings, device: event.target.value as DeviceSettings['device'] })}>
          <option value="cpu">CPU</option><option value="auto">{t('Авто')}</option><option value="gpu">GPU</option>
        </select></label>
        <label>{t('Устройство для поиска')}<select aria-label={t('Устройство для поиска')} disabled={op.busy} value={settings.search_device ?? 'cpu'}
          onChange={event => setSettings({ ...settings, search_device: event.target.value as DeviceSettings['search_device'] })}>
          <option value="cpu">CPU</option><option value="auto">{t('Авто')}</option><option value="gpu">GPU</option>
        </select></label>
      </div>
      <p className="baseline-note">{t('Вы можете продолжить индексацию и поиск на другом устройстве.')}</p>
      <button className="primary" disabled={op.busy || !valid} onClick={save}>{op.busy ? t('Проверяем устройство…') : t('Применить устройство')}</button>
    </>}
    {execution?.warning && <p className="warning">{t(execution.warning)}</p>}
    {notice && <p role="status">{t(notice)}</p>}{op.error && <p className="error" role="alert">{t(op.error)}</p>}
  </section>;
}
