import { t } from './i18n';

export function estimatedTime(seconds: number | null | undefined): string {
  if (seconds == null) return t('Оценка появится после первых батчей.');
  if (seconds <= 0) return t('Индексация завершена.');
  const minutes = Math.ceil(seconds / 60);
  if (minutes < 60) return t('Осталось примерно {p0} мин.', { p0: minutes });
  return t('Осталось примерно {p0} ч {p1} мин.', { p0: Math.floor(minutes / 60), p1: minutes % 60 });
}
