import { useEffect, useState } from 'react';
import { api } from './api';
import { t } from './i18n';
import { useDialogOperation } from './useDialogOperation';
import type { Mutation } from './IndexCard';
import type { MediaStatus } from './types';

type Props = Mutation & { media: MediaStatus; chatId: string; busy: boolean; onSaved: (value: unknown) => void };

export default function OcrBatchSettings({ media, chatId, busy, onSaved, onStart, onEnd }: Props) {
  const [images, setImages] = useState(media.ocr_batch_size ?? 1);
  const [regions, setRegions] = useState(media.ocr_region_batch_size ?? 8);
  const [dirty, setDirty] = useState(false);
  const op = useDialogOperation();
  useEffect(() => { if (!dirty) { setImages(media.ocr_batch_size ?? 1); setRegions(media.ocr_region_batch_size ?? 8); } }, [media.ocr_batch_size, media.ocr_region_batch_size, dirty]);
  const disabled = busy || op.busy;
  const valid = Number.isInteger(images) && images >= 1 && images <= 4 && Number.isInteger(regions) && regions >= 1 && regions <= 32;
  const save = () => void op.run(async current => {
    onStart?.();
    try {
      const value = await api(`/api/chats/${encodeURIComponent(chatId)}/index/settings`, { method: 'PATCH', body: JSON.stringify({ ocr_batch_size: images, ocr_region_batch_size: regions }) });
      if (current()) { setDirty(false); onSaved(value); }
    } finally { onEnd?.(); }
  });
  return <details className="index-advanced"><summary>{t('Батчи OCR')}</summary>
    <div className="index-batch">
      <label>{t('Изображений в батче OCR')}<input aria-label={t('Изображений в батче OCR')} type="number" min="1" max="4" step="1" value={images} disabled={disabled} onChange={event => { setImages(Number(event.target.value)); setDirty(true); }} /></label>
      <div className="batch-presets">{[1, 2, 4].map(value => <button key={value} aria-pressed={images === value} disabled={disabled} onClick={() => { setImages(value); setDirty(true); }}>{value}</button>)}</div>
      <label>{t('Областей текста в батче OCR')}<input aria-label={t('Областей текста в батче OCR')} type="number" min="1" max="32" step="1" value={regions} disabled={disabled} onChange={event => { setRegions(Number(event.target.value)); setDirty(true); }} /></label>
      <div className="batch-presets">{[1, 4, 8, 16, 32].map(value => <button key={value} aria-pressed={regions === value} disabled={disabled} onClick={() => { setRegions(value); setDirty(true); }}>{value}</button>)}</div>
      <small>{t('Межкартинные батчи работают с PaddleOCR. Готовый кэш сохраняется при изменении батчей.')}</small>
      {dirty && <button className="index-save" disabled={disabled || !valid} onClick={save}>{t('Применить батчи OCR')}</button>}
    </div>
    {op.error && <p className="error" role="alert">{t(op.error)}</p>}
  </details>;
}
