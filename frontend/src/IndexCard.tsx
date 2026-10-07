import { useEffect, useState } from 'react';
import { api } from './api';
import { t } from './i18n';
import { estimatedTime } from './estimatedTime';
import { useDialogOperation } from './useDialogOperation';
import type { ReactNode } from 'react';

export type Mutation = { onStart?: () => void; onEnd?: () => void; pending?: boolean };
type Props = Mutation & {
  title: string; model: string; ready: number; total: number; paused: boolean;
  enabled: boolean; preparing: boolean; seconds?: number | null; chatId?: string;
  batch: number; kind: 'embedding_batch' | 'image_batch'; busy: boolean;
  onPrepare: () => void; onControl: (action: string) => void; onSaved: (value: unknown) => void;
  children?: ReactNode;
};

export default function IndexCard(props: Props) {
  const { title, model, ready, total, paused, enabled, preparing, seconds, chatId, kind } = props;
  const [batch, setBatch] = useState(props.batch);
  const [dirty, setDirty] = useState(false);
  const op = useDialogOperation();
  useEffect(() => { if (!dirty) setBatch(props.batch); }, [props.batch, dirty]);
  const max = kind === 'embedding_batch' ? 128 : 32;
  const presets = kind === 'embedding_batch' ? [4, 8, 16, 32, 64, 128] : [1, 2, 4, 8, 16, 32];
  const busy = props.busy || op.busy || preparing;
  const valid = Number.isInteger(batch) && batch >= 1 && batch <= max;
  const save = () => void op.run(async current => {
    props.onStart?.();
    try {
      const value = await api(chatId ? `/api/chats/${encodeURIComponent(chatId)}/index/settings` : '/api/settings', {
        method: 'PATCH', body: JSON.stringify({ [kind]: batch }),
      });
      if (current()) { setDirty(false); props.onSaved(value); }
    } finally { props.onEnd?.(); }
  });
  return <section className="index-card" aria-label={`${title} · ${model}`}>
    <header className="index-card-heading"><h3>{title}</h3><span className="index-model">{model}</span></header>
    <div className="index-progress-label"><span>{t('Готово: {p0} / {p1}', { p0: ready, p1: total })}</span>
      <span>{preparing ? t('Подготавливаем…') : paused ? t('На паузе') : enabled ? ready >= total ? t('Готово') : t('Индексация') : t('Не подготовлен')}</span></div>
    <progress aria-label={t('Прогресс индексации {p0}', { p0: title })} value={ready} max={Math.max(1, total)} />
    {enabled && <p className="index-eta" role="status">{estimatedTime(seconds)}</p>}
    <div className="index-batch">
      <label>{t('Размер батча')}<input aria-label={t('Размер батча {p0}', { p0: title })} type="number" min="1" max={max} step="1"
        value={batch} disabled={busy} onChange={event => { setBatch(Number(event.target.value)); setDirty(true); }} /></label>
      <div className="batch-presets" aria-label={t('Быстрый выбор размера батча')}>
        {presets.map(value => <button type="button" key={value} aria-pressed={batch === value} disabled={busy}
          onClick={() => { setBatch(value); setDirty(true); }}>{value}</button>)}
      </div>
      {dirty && <button className="index-save" disabled={busy || !valid} onClick={save}>{t('Применить батч')}</button>}
      <small>{t('Больший батч использует больше памяти. При нехватке памяти он уменьшается автоматически.')}</small>
    </div>
    <div className="job-actions index-actions">
      {enabled ? <button className="primary" disabled={busy} onClick={() => props.onControl(paused ? 'resume' : 'pause')}>
        {paused ? t('Продолжить индексацию') : t('Пауза индексации')}</button> :
        <button className="primary" disabled={busy} onClick={props.onPrepare}>{t('Открыть настройки моделей')}</button>}
      {enabled && <button disabled={busy} onClick={() => props.onControl('retry')}>{t('Повторить ошибки')}</button>}
    </div>
    {op.error && <p className="error" role="alert">{t(op.error)}</p>}
    {props.children}
  </section>;
}
