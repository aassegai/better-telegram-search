import type { ReactNode } from 'react';
import { t } from './i18n';

export default function ModelCard({ title, purpose, ready, preparing, children, details, device, actions, options, error, download }: {
  title: string; purpose: string; ready: boolean; preparing: boolean; children: ReactNode;
  details?: ReactNode; device: ReactNode; actions: ReactNode; options?: ReactNode; error?: string | null;
  download?: { completed: number; total: number };
}) {
  return <section className="model-card" aria-label={t(title)}>
    <header className="model-card-heading"><h3>{t(title)}</h3><span className="model-state">{preparing ? t('Подготавливаем…') : error ? t('Ошибка') : ready ? t('Готова') : t('Не подготовлена')}</span></header>
    <p className="model-purpose">{t(purpose)}</p>
    {children}
    {details && <details className="model-details"><summary>{t('Подробности модели')}</summary>{details}</details>}
    {options}
    {device}
    <div className="model-actions">{actions}</div>
    {download && preparing && <div className="model-download"><progress aria-label={t('Загрузка модели')}
      value={download.total > 0 ? download.completed : undefined} max={download.total > 0 ? download.total : undefined} />
      {download.total > 0 && <small>{t('{p0} / {p1} МиБ', { p0: (download.completed / 1024 ** 2).toFixed(0), p1: (download.total / 1024 ** 2).toFixed(0) })}</small>}</div>}
    {error && <p className="error" role="alert">{t(error)}</p>}
  </section>;
}
