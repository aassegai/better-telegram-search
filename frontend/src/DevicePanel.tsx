import { useEffect, useRef, useState } from 'react';
import { api } from './api';
import { t } from './i18n';
import { useDialogOperation } from './useDialogOperation';
import { useModelSettings } from './ModelSettings';
import type { Device, ModelRole, ModelSettings } from './ModelSettings';

export default function DevicePanel({ model, onChange }: {
  model: ModelRole; onChange: (current: () => boolean) => void | Promise<void>;
}) {
  const { models, update } = useModelSettings();
  const settings = models?.[model];
  const supported = model === 'ocr' ? settings?.profile_devices?.[settings.engine ?? ''] ?? (settings?.engine === 'tesseract' ? ['cpu'] : ['cpu', 'gpu']) : ['cpu', 'gpu'];
  const allowed = ['cpu', 'gpu', 'auto', ...(model === 'ocr' ? ['hybrid'] : [])].filter(device => device === 'auto' ? supported.includes('gpu') || (settings?.engine?.startsWith('custom:') && supported.includes('cpu')) : device === 'hybrid' ? supported.includes('cpu') && supported.includes('gpu') : supported.includes(device));
  const [draft, setDraft] = useState<{ device: Device; search_device: Device } | null>(null);
  const [dirty, setDirty] = useState(false);
  const previousEngine = useRef(settings?.engine);
  const [notice, setNotice] = useState('');
  const [warning, setWarning] = useState('');
  const op = useDialogOperation();
  useEffect(() => {
    if (!settings) return;
    if (!dirty || previousEngine.current !== settings.engine) {
      setDraft({ device: settings.device, search_device: settings.search_device ?? 'cpu' });
      setDirty(false); previousEngine.current = settings.engine;
    }
  }, [settings, dirty]);
  const save = () => void op.run(async current => {
    if (!draft || !settings) return;
    const result = await api<{ model: ModelSettings; execution: { warning: string | null }; query_execution: { warning: string | null } }>(`/api/models/${model}/device`, {
      method: 'POST', body: JSON.stringify({ device: draft.device,
        ...(model === 'ocr' ? { ocr_engine: settings.engine } : { search_device: draft.search_device }) }),
    });
    if (current()) {
      update(model, result.model); setDirty(false);
      setWarning(result.execution.warning || result.query_execution.warning || '');
      setNotice('Устройства сохранены.'); await onChange(current);
    }
  });
  const title = model === 'e5' ? 'Устройства текстовой модели' : model === 'clip' ? 'Устройства визуальной модели' : 'Устройства OCR';
  const change = (key: 'device' | 'search_device', value: string) => { if (draft) { setDraft({ ...draft, [key]: value }); setDirty(true); setNotice(''); } };
  return <section className="device-panel" aria-label={t(title)}><h3>{t(title)}</h3>
    {draft && settings ? <>
      <div className="resource-fields">
        <label>{t(model === 'ocr' ? 'Устройство распознавания' : 'Устройство для индексации')}<select
          aria-label={t(model === 'ocr' ? 'Устройство распознавания' : 'Устройство для индексации')}
          disabled={op.busy} value={draft.device} onChange={event => change('device', event.target.value)}>
          {!allowed.includes(draft.device) && <option value={draft.device} disabled>{t('Недоступно: {p0}', { p0: draft.device.toUpperCase() })}</option>}
          {allowed.includes('cpu') && <option value="cpu">CPU</option>}
          {allowed.includes('auto') && <option value="auto">{t('Авто')}</option>}
          {allowed.includes('gpu') && <option value="gpu">GPU</option>}
          {allowed.includes('hybrid') && <option value="hybrid">CPU + GPU</option>}
        </select></label>
        {model !== 'ocr' && <label>{t('Устройство для поиска')}<select aria-label={t('Устройство для поиска')}
          disabled={op.busy} value={draft.search_device} onChange={event => change('search_device', event.target.value)}>
          <option value="cpu">CPU</option><option value="auto">{t('Авто')}</option><option value="gpu">GPU</option>
        </select></label>}
      </div>
      {model === 'ocr' && settings.engine === 'tesseract' && <p className="baseline-note">{t('Tesseract работает на CPU. Для GPU выберите PaddleOCR в разделе «Модели».')}</p>}
      {model === 'ocr' && draft.device === 'hybrid' && <p className="baseline-note">{t('CPU и GPU распознают разные изображения одновременно. Пауза OCR останавливает оба устройства.')}</p>}
      <button className="primary" disabled={op.busy || !dirty} onClick={save}>{op.busy ? t('Проверяем устройство…') : t('Сохранить устройства')}</button>
    </> : <p role="status">{t('Проверяем…')}</p>}
    {warning && <p className="warning">{t(warning)}</p>}{notice && <p role="status">{t(notice)}</p>}
    {op.error && <p className="error" role="alert">{t(op.error)}</p>}
  </section>;
}
