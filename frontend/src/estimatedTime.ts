import { t, uiLocale } from './i18n';
import type { IndexEstimate } from './types';

export function estimatedTime(seconds: number | null | undefined, unknown = t('Оценка появится после первых батчей.'), estimate?: IndexEstimate | null): string {
  if (estimate?.paused) return t('Продолжите индексацию для оценки времени.');
  if (estimate?.waiting_reason === 'memory') return t('Оценка времени недоступна: индексация ожидает памяти.');
  if (estimate?.stale) return t('Оценка уточняется: давно не было завершённых батчей.');
  if (seconds != null && Number.isFinite(seconds) && seconds <= 0) return t('Индексация завершена.');
  const lower = estimate?.lower_seconds, upper = estimate?.upper_seconds;
  if (lower != null && upper != null && Number.isFinite(lower) && Number.isFinite(upper) && lower >= 0 && upper > 0 && upper >= lower) {
    if (upper < 60) return t('Осталось меньше минуты.');
    const step = upper < 600 ? 1 : upper < 3600 ? 5 : 15;
    const lo = Math.max(1, Math.floor(lower / 60 / step) * step);
    const hi = Math.max(lo + step, Math.ceil(upper / 60 / step) * step);
    if (hi < 120) return t('Осталось примерно {p0}–{p1} мин.', { p0: lo, p1: hi });
    const format = new Intl.NumberFormat(uiLocale(), { maximumFractionDigits: 1 });
    return t('Осталось примерно {p0}–{p1} ч.', { p0: format.format(lo / 60), p1: format.format(hi / 60) });
  }
  if (seconds == null || !Number.isFinite(seconds)) return unknown;
  const minutes = Math.ceil(seconds / 60);
  if (minutes < 60) return t('Осталось примерно {p0} мин.', { p0: minutes });
  return t('Осталось примерно {p0} ч {p1} мин.', { p0: Math.floor(minutes / 60), p1: minutes % 60 });
}
