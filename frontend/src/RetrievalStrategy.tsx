import { t } from './i18n';
import type { RetrievalInfo } from './types';

const fallbackLabels: Record<string, string> = {
  ann_index_unavailable: 'Индекс ANN ещё не подготовлен.',
  ann_index_incompatible: 'Индекс ANN не подходит к текущей модели.',
  ann_dimension_mismatch: 'Индекс ANN не подходит к текущей модели.',
  ann_scope_too_small: 'Для небольшой области выполнен полный поиск.',
  ann_table_too_small: 'Векторов пока недостаточно для ANN.',
  ann_memory_limit: 'Достигнут лимит памяти ANN.',
  ann_query_timeout: 'ANN не завершился за отведённое время.',
  ann_worker_stopping: 'Процесс ANN ещё останавливается.',
  ann_scope_changed: 'Область ANN потребовала повторной проверки.',
  ann_scope_unavailable: 'Для выбранных фильтров выполнен полный поиск.',
};

export function RetrievalChoice({ value, onChange, disabledReason, busy, id }: {
  value: 'exact' | 'ann'; onChange: (value: 'exact' | 'ann') => void;
  disabledReason: string | null; busy: boolean; id: string;
}) {
  return <fieldset className="retrieval-choice" disabled={busy || !!disabledReason} aria-describedby={id}>
    <legend>{t('Полнота поиска')}</legend>
    <div className="retrieval-options">
      <button type="button" aria-pressed={value === 'exact'} onClick={() => onChange('exact')}>{t('Полнее · полный поиск')}</button>
      <button type="button" aria-pressed={value === 'ann'} onClick={() => onChange('ann')}>{t('Быстрее · ANN')}</button>
    </div>
    <p id={id} className="baseline-note">{t(disabledReason ?? 'ANN может пропустить близкие совпадения. Полный поиск сравнивает все доступные векторы выбранной области и обычно работает дольше.')}</p>
    {!disabledReason && value === 'ann' && <p className="baseline-note">{t('Подготовка ANN и лимиты — в настройках производительности. Без готового ANN выполняется полный поиск.')}</p>}
  </fieldset>;
}

export function RetrievalSummary({ info }: { info: RetrievalInfo | null }) {
  const effective = info?.effective_strategy;
  if (!effective || effective === 'none') return null;
  const fallback = info?.requested_strategy === 'ann' && (info.fallback_reasons?.length ?? 0) > 0;
  const label = effective === 'ann' ? 'Выполнен поиск ANN' : effective === 'mixed' ? 'Выполнены ANN и полный поиск' : 'Выполнен полный поиск';
  const explanations = [...new Set(info?.fallback_reasons?.map(reason => fallbackLabels[reason] ?? 'ANN недоступен; выполнен полный поиск.'))];
  return <div className="retrieval-summary" role="status">
    <span>{t(label)}</span>{fallback && <span>{explanations.map(message => t(message)).join(' ')}</span>}
  </div>;
}
