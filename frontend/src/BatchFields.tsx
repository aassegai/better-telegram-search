import { t } from './i18n';

export const batchFields = [
  ['embedding_batch', 'Батч текста', 128, [4, 8, 16, 32, 64, 128]],
  ['image_batch', 'Батч изображений', 32, [1, 2, 4, 8, 16, 32]],
  ['ocr_batch_size', 'Изображений в батче OCR', 4, [1, 2, 4]],
  ['ocr_region_batch_size', 'Областей текста в батче OCR', 32, [1, 4, 8, 16, 32]],
] as const;
export type BatchKey = typeof batchFields[number][0];
export type Batches = Record<BatchKey, number>;
export type Overrides = Record<BatchKey, number | null>;

export function validBatches(values: Overrides, inherited: boolean) {
  return batchFields.every(([key, , max]) => values[key] === null ? inherited
    : Number.isInteger(values[key]) && values[key]! >= (key === 'ocr_region_batch_size' && !inherited ? 0 : 1) && values[key]! <= max);
}

export default function BatchFields({ values, defaults, onChange, disabled, inherit = false }: {
  values: Overrides; defaults: Batches; onChange: (key: BatchKey, value: number | null) => void;
  disabled: boolean; inherit?: boolean;
}) {
  return <div className="batch-fields">{batchFields.map(([key, label, max, presets]) => {
    const inherited = values[key] === null;
    const value = values[key] ?? defaults[key];
    return <section className="batch-field" key={key} aria-label={t(label)}>
      <label>{t(label)}<input type="number" aria-label={t(label)} min={key === 'ocr_region_batch_size' && !inherit ? 0 : 1} max={max} step="1"
        disabled={disabled} value={inherited && value === 0 ? '' : value} placeholder={value === 0 ? t('Авто') : undefined}
        onChange={event => onChange(key, Number(event.target.value))} /></label>
      <div className="batch-presets">{presets.map(preset => <button key={preset} type="button" disabled={disabled}
        aria-pressed={!inherited && value === preset} onClick={() => onChange(key, preset)}>{preset}</button>)}</div>
      {inherit && <div className="inheritance"><span>{inherited ? t('Общее значение: {p0}', { p0: defaults[key] || t('Авто') }) : t('Переопределено для этого диалога')}</span>
        <button type="button" className="text-button" disabled={disabled || inherited} onClick={() => onChange(key, null)}>{t('Использовать общее значение')}</button></div>}
    </section>;
  })}<p className="baseline-note">{t('Больший батч использует больше памяти. При нехватке памяти он уменьшается автоматически.')}</p></div>;
}
