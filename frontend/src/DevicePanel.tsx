import { useEffect, useState } from 'react';
import { api } from './api';
import { t } from './i18n';
import { useDialogOperation } from './useDialogOperation';

type Device = 'cpu' | 'auto' | 'gpu';
type ModelSettings = { device: Device; search_device?: Device; engine?: 'tesseract' | 'paddle'; paths: string[]; profile_paths?: Record<string, string> };
type Execution = { warning: string | null };

export default function DevicePanel({ model, profile, onChange }: {
  model: 'e5' | 'clip' | 'ocr'; profile?: string; onChange: (current: () => boolean) => void | Promise<void>;
}) {
  const [settings, setSettings] = useState<ModelSettings | null>(null);
  const [notice, setNotice] = useState('');
  const [warning, setWarning] = useState('');
  const op = useDialogOperation();
  useEffect(() => {
    let alive = true;
    api<Record<string, ModelSettings>>('/api/models').then(value => { if (alive) setSettings(value[model]); })
      .catch(error => { if (alive) setNotice(error instanceof Error ? error.message : t('Не удалось прочитать настройки.')); });
    return () => { alive = false; };
  }, [model]);
  const save = () => void op.run(async current => {
    if (!settings) return;
    const result = await api<{ model: ModelSettings; execution: Execution; query_execution: Execution }>(`/api/models/${model}/device`, {
      method: 'POST', body: JSON.stringify({ device: settings.device,
        ...(model === 'ocr' ? { ocr_engine: settings.engine } : { search_device: settings.search_device ?? 'cpu' }) }),
    });
    if (current()) {
      setSettings(result.model); setWarning(result.execution.warning || result.query_execution.warning || '');
      setNotice(t('Устройства сохранены.')); await onChange(current);
    }
  });
  const paths = profile && settings?.profile_paths?.[profile] ? [settings.profile_paths[profile]] : settings?.paths;
  return <section className="device-panel" aria-label={t('Устройства {p0}', { p0: model.toUpperCase() })}>
    {settings && <>
      {model === 'ocr' && <label>{t('Модель OCR')}<select aria-label={t('Модель OCR')} disabled={op.busy} value={settings.engine}
        onChange={event => setSettings({ ...settings, engine: event.target.value as ModelSettings['engine'],
          ...(event.target.value === 'tesseract' ? { device: 'cpu' as const } : {}) })}>
        <option value="tesseract">Tesseract · CPU</option><option value="paddle">PaddleOCR · CPU / GPU</option>
      </select></label>}
      <div className="resource-fields">
        <label>{t(model === 'ocr' ? 'Устройство распознавания' : 'Устройство для индексации')}<select
          aria-label={t(model === 'ocr' ? 'Устройство распознавания' : 'Устройство для индексации')}
          disabled={op.busy} value={settings.device} onChange={event => setSettings({ ...settings,
            device: event.target.value as Device,
            ...(model === 'ocr' && event.target.value !== 'cpu' ? { engine: 'paddle' as const } : {}) })}>
          <option value="cpu">CPU</option><option value="auto">{t('Авто')}</option><option value="gpu">GPU</option>
        </select></label>
        {model !== 'ocr' && <label>{t('Устройство для поиска')}<select aria-label={t('Устройство для поиска')}
          disabled={op.busy} value={settings.search_device ?? 'cpu'}
          onChange={event => setSettings({ ...settings, search_device: event.target.value as Device })}>
          <option value="cpu">CPU</option><option value="auto">{t('Авто')}</option><option value="gpu">GPU</option>
        </select></label>}
      </div>
      {model !== 'ocr' ? <p className="baseline-note">{t('Вы можете продолжить индексацию и поиск на другом устройстве.')}</p>
        : <p className="baseline-note">{t('PaddleOCR использует общий кэш на CPU и GPU. При смене OCR-модели распознавание выполняется заново; прежний кэш сохраняется.')}</p>}
      <button className="primary" disabled={op.busy} onClick={save}>{op.busy ? t('Проверяем устройство…') : t('Применить устройство')}</button>
      <div className="model-paths"><span>{t('Папка загрузки модели')}</span>{paths?.map(path => <code key={path}>{path}</code>)}</div>
    </>}
    {warning && <p className="warning">{t(warning)}</p>}
    {notice && <p role="status">{t(notice)}</p>}{op.error && <p className="error" role="alert">{t(op.error)}</p>}
  </section>;
}
