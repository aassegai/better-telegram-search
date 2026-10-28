import { t } from './i18n';
import { estimatedTime } from './estimatedTime';
import type { ReactNode } from 'react';
import IndexErrors from './IndexErrors';
import type { IndexEstimate } from './types';

export type Mutation = { onStart?: () => void; onEnd?: () => void; pending?: boolean };
type Props = {
  title: string; model: string; ready: number; total: number; paused: boolean;
  enabled: boolean; preparing: boolean; seconds?: number | null; chatId?: string;
  blocked?: boolean; failed?: number; estimate?: IndexEstimate | null; maintenance?: boolean;
  batch: number; busy: boolean; onPerformance?: () => void;
  onPrepare: () => void; onControl: (action: string) => void; children?: ReactNode;
};

export default function IndexCard(props: Props) {
  const { title, model, ready, total, paused, enabled, preparing, seconds } = props;
  const busy = props.busy || preparing;
  return <section className="index-card" aria-label={`${title} · ${model}`}>
    <header className="index-card-heading"><h3>{title}</h3><span className="index-model">{model}</span></header>
    <div className="index-progress-label"><span>{t('Готово: {p0} / {p1}', { p0: ready, p1: total })}</span>
      <span>{preparing ? t('Подготавливаем…') : props.blocked ? t('Ошибка индексации') : props.maintenance ? t('Уплотнение индекса') : paused ? t('На паузе') : enabled ? ready >= total ? t('Готово') : props.estimate?.waiting_reason === 'database' ? t('Ожидание базы данных') : props.estimate?.waiting_reason === 'memory' ? t('Ожидание RAM') : t('Индексация') : t('Не подготовлен')}</span></div>
    <progress aria-label={t('Прогресс индексации {p0}', { p0: title })} value={ready} max={Math.max(1, total)} />
    {enabled && !props.blocked && !preparing && !props.maintenance && props.estimate?.waiting_reason !== 'database' && <p className="index-eta" role="status">{paused && ready < total ? t('Продолжите индексацию для оценки времени.') : estimatedTime(seconds, undefined, props.estimate)}</p>}
    <IndexErrors count={props.failed ?? 0} busy={busy || !enabled} onRetry={() => props.onControl('retry')} />
    <div className="job-actions index-actions">
      {enabled ? !props.blocked && <button className="primary" disabled={busy} onClick={() => props.onControl(paused ? 'resume' : 'pause')}>
        {paused ? t('Продолжить индексацию') : t('Пауза индексации')}</button> :
        <button className="primary" disabled={busy} onClick={props.onPrepare}>{t('Открыть настройки моделей')}</button>}
      {props.onPerformance && <button type="button" className="text-button" onClick={props.onPerformance}>{t('Настройки производительности')}</button>}
    </div>
    {props.children}
  </section>;
}
