import type { ReactNode } from 'react';
import { t } from './i18n';

export default function IndexErrors({ count, busy, onRetry, children }: {
  count: number; busy: boolean; onRetry: () => void; children?: ReactNode;
}) {
  if (count <= 0) return null;
  return <div className="index-errors">
    <strong role="status">{t('Задач с ошибкой: {p0}', { p0: count })}</strong>
    {children}
    <p>{t('Эти задачи не обработаны. Повторите ошибки — готовые результаты сохранятся.')}</p>
    <button className="primary" disabled={busy} onClick={onRetry}>
      {t('Повторить ошибки ({p0})', { p0: count })}
    </button>
  </div>;
}
