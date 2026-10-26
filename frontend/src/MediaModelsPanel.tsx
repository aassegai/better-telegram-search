import { useEffect, useRef, useState } from 'react';
import { api } from './api';
import { t } from './i18n';
import type { MediaStatus } from './types';
import { useDialogOperation } from './useDialogOperation';
import ModelCard from './ModelCard';
import { ModelDeviceSummary, ModelPaths, useModelSettings } from './ModelSettings';
import type { ModelSettings } from './ModelSettings';

export default function MediaModelsPanel({ media, onMediaChange, onPerformance }: {
  media: MediaStatus | null; onMediaChange: (status: MediaStatus) => void; onPerformance: () => void;
}) {
  const { models, update } = useModelSettings();
  const [profile, setProfile] = useState(media?.images_enabled ? media.visual_profile ?? 'clip' : 'siglip2');
  const chosen = useRef(false);
  useEffect(() => { if (media?.images_enabled && !chosen.current) setProfile(media.visual_profile ?? 'clip'); }, [media?.images_enabled, media?.visual_profile]);
  const [engine, setEngine] = useState<'tesseract' | 'paddle'>('tesseract');
  const engineChosen = useRef(false);
  useEffect(() => { if (models?.ocr.engine && !engineChosen.current) setEngine(models.ocr.engine); }, [models?.ocr.engine]);
  const [reindex, setReindex] = useState(false);
  const [offline, setOffline] = useState(false);
  const [ocrOffline, setOcrOffline] = useState(false);
  const [notice, setNotice] = useState('');
  const [operationRole, setOperationRole] = useState<'ocr' | 'images'>('ocr');
  const op = useDialogOperation();
  const preparing = media?.preparation_state === 'preparing';
  const disabled = op.busy || preparing;
  const changing = Boolean(media?.images_enabled && (media.visual_profile ?? 'clip') !== profile);
  const ready = Boolean(media?.images_available && !changing);
  const engineDirty = Boolean(models?.ocr && models.ocr.engine !== engine);
  const bytes = media?.visual_profiles?.find(item => item.profile === profile)?.download_bytes;
  const prepare = (kind: 'ocr' | 'images') => { setOperationRole(kind); void op.run(async current => {
    const value = await api<MediaStatus>('/api/media-index/prepare', { method: 'POST', body: JSON.stringify({
      kind, offline: kind === 'ocr' ? ocrOffline : offline,
      ...(kind === 'images' ? { profile, reindex: changing && reindex } : {}),
    }) });
    if (current()) onMediaChange(value);
  }); };
  return <>
    {media?.error && <p className="error" role="alert">{t(media.error)}</p>}
    {preparing && !media?.preparation_kind && <p role="status">{t('Подготавливаем модели…')}</p>}
    <ModelCard title="Модель изображений" purpose="Поиск фотографий по описанию."
      ready={ready} preparing={Boolean(preparing && media?.preparation_kind === 'images')} error={operationRole === 'images' ? op.error : null}
      details={<><ModelPaths role="clip" profile={profile} /><p>ONNX Runtime · {profile === 'siglip2' ? 'SigLIP 2 · 224 × 224' : 'CLIP · legacy'}</p></>}
      device={<ModelDeviceSummary role="clip" onPerformance={onPerformance} />} download={preparing ? { completed: 0, total: 0 } : undefined}
      options={<>
        {changing && <label className="check-label"><input type="checkbox" checked={reindex} disabled={disabled} onChange={event => setReindex(event.target.checked)} />{t('Перестроить индекс изображений при смене модели; кэш OCR сохранится')}</label>}
        <label className="check-label"><input type="checkbox" checked={offline} disabled={disabled} onChange={event => setOffline(event.target.checked)} />{t('Только локальный кэш моделей')}</label>
      </>}
      actions={<button className={changing ? 'danger' : 'primary'} disabled={disabled || ready || (changing && !reindex)}
        onClick={() => prepare('images')}>{changing ? t('Сменить модель и перестроить изображения') : t('Подготовить модель изображений')}</button>}>
      <label>{t('Визуальная модель')}<select aria-label={t('Визуальная модель')} value={profile} disabled={disabled}
        onChange={event => { chosen.current = true; setProfile(event.target.value as 'clip' | 'siglip2'); setReindex(false); }}>
        {media?.images_enabled === 1 && media.visual_profile === 'clip' && <option value="clip" disabled>CLIP · legacy</option>}
        <option value="siglip2">SigLIP 2</option>
      </select></label>
      {media?.images_enabled === 1 && media.visual_profile === 'clip' && <p className="warning">{t('Существующий индекс использует прежнюю модель. Его можно продолжать; переход на другую модель требует перестройки.')}</p>}
      <p>{t('Источник модели')}: {t('Встроенная загрузка')}</p>
      {bytes != null && <small>{t('Размер загрузки: {p0} МиБ', { p0: (bytes / 1024 ** 2).toFixed(0) })}</small>}
    </ModelCard>
    <ModelCard title="Модель распознавания · OCR" purpose="Распознавание текста на фотографиях для словесного и смыслового поиска."
      ready={Boolean(media?.ocr_available && !engineDirty)} preparing={Boolean(preparing && media?.preparation_kind === 'ocr')} error={operationRole === 'ocr' ? op.error : null}
      details={<><ModelPaths role="ocr" /><p>{t('PaddleOCR использует общий кэш на CPU и GPU. При смене OCR-модели распознавание выполняется заново; прежний кэш сохраняется.')}</p></>}
      device={<ModelDeviceSummary role="ocr" onPerformance={onPerformance} />} download={preparing ? { completed: 0, total: 0 } : undefined}
      options={<label className="check-label"><input type="checkbox" checked={ocrOffline} disabled={disabled} onChange={event => setOcrOffline(event.target.checked)} />{t('Только локальный кэш моделей')}</label>}
      actions={<>
        <button className="secondary" disabled={disabled || !engineDirty || !models?.ocr} onClick={() => { setOperationRole('ocr'); void op.run(async current => {
          if (!models?.ocr) return;
          const value = await api<{ model: ModelSettings }>('/api/models/ocr/device', { method: 'POST', body: JSON.stringify({
            device: engine === 'tesseract' ? 'cpu' : models.ocr.device, ocr_engine: engine,
          }) });
          if (!current()) return;
          update('ocr', value.model); setNotice('Модель OCR выбрана. Подготовка запускается отдельно.');
          const status = await api<MediaStatus>('/api/media-index'); if (current()) onMediaChange(status);
        }); }}>{t('Сохранить выбор OCR')}</button>
        <button className="primary" disabled={disabled || engineDirty || !media?.ocr_runtime_installed || Boolean(media.ocr_available)} onClick={() => prepare('ocr')}>{t('Подготовить OCR')}</button>
      </>}>
      <label>{t('Модель OCR')}<select aria-label={t('Модель OCR')} value={engine} disabled={disabled || !models?.ocr}
        onChange={event => { engineChosen.current = true; setEngine(event.target.value as 'tesseract' | 'paddle'); setNotice(''); }}>
        <option value="tesseract">Tesseract · CPU</option><option value="paddle">PaddleOCR · CPU / GPU</option>
      </select></label>
      <p>{t('Источник модели')}: {t('Встроенная загрузка')}</p>
      {media && !media.ocr_runtime_installed && <p className="warning">{t('Установите OCR runtime: uv sync --locked --extra semantic --extra ocr.')}</p>}
      {notice && <p role="status">{t(notice)}</p>}
    </ModelCard>
  </>;
}
