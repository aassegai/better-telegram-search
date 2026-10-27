import { t, uiLocale } from './i18n';
import type { SearchJobStatus } from './types';
import { RetrievalSummary } from './RetrievalStrategy';

const phases: Record<string, string> = {
  queued: 'Ожидание в очереди', scope: 'Подготовка области поиска', query: 'Подготовка запроса',
  query_text: 'Подготовка текстового запроса', query_images: 'Подготовка описания изображения',
  query_ocr: 'Подготовка запроса к OCR', browse: 'Загрузка сообщений', text_words: 'Поиск в сообщениях',
  text_scope: 'Отбор текстовых фрагментов', text_word_mapping: 'Подбор фрагментов по совпадениям',
  text_results: 'Сборка текстовой выдачи', image_scope: 'Отбор изображений', ocr_scope: 'Отбор фрагментов OCR',
  image_results: 'Сборка выдачи изображений', ocr_results: 'Сборка выдачи OCR',
  media_coverage: 'Проверка покрытия медиаиндекса',
  ocr_tokens: 'Поиск слов в OCR', ocr_substrings: 'Поиск подстрок в OCR',
  ocr_typos: 'Поиск вариантов с опечатками в OCR', ocr_verify: 'Проверка совпадений OCR',
  text_vectors: 'Сравнение текстовых фрагментов', image_vectors: 'Сравнение изображений',
  ocr_words: 'Поиск в распознанном тексте', ocr_vectors: 'Сравнение распознанного текста',
  rerank_documents: 'Подготовка кандидатов для уточнения', rerank_setup: 'Загрузка модели уточнения',
  rerank_query: 'Подготовка уточнения порядка', rerank_passage: 'Уточнение порядка результатов',
  combine: 'Объединение совпадений', render: 'Подготовка выдачи',
  text_vectors_scope: 'Проверка области текстового ANN', image_vectors_scope: 'Проверка области ANN изображений',
  ocr_vectors_scope: 'Проверка области ANN распознанного текста',
};

export default function SearchProgress({ status, onStop }: {
  status: SearchJobStatus | null; onStop: () => void;
}) {
  const stopping = status?.state === 'cancelling';
  const known = status?.total != null && status.total > 0;
  return <section className="search-progress" role="region" aria-label={t('Выполнение поиска')}>
    <div className="search-progress-heading">
      <span role="status">{stopping ? t('Останавливаем…') : t(phases[status?.phase ?? 'queued'] ?? 'Ищем…')}</span>
      <button type="button" onClick={onStop} disabled={!status || stopping}>{t('Остановить')}</button>
    </div>
    <progress aria-label={t('Прогресс текущего этапа')} max={known ? status!.total! : undefined}
      value={known ? status!.completed : undefined} />
    {status && <div className="search-progress-measurements">
      {status.total != null && <span>{status.completed.toLocaleString(uiLocale())} / {status.total.toLocaleString(uiLocale())}
        {status.unit === 'vectors' ? t(' сравнений') : status.unit === 'texts' ? t(' текстов') : status.unit === 'chunks' ? t(' фрагментов') : status.unit === 'images' ? t(' изображений') : t(' сообщений')}</span>}
      {status.total == null && status.unit === 'native_queries' && <span>{t('Завершено вызовов ANN: {p0}', { p0: status.completed })}</span>}
      {status.total == null && status.unit === 'ids' && <span>{t('Проверено записей: {p0}', { p0: status.completed.toLocaleString(uiLocale()) })}</span>}
      <span>{t('Прошло {p0} с', { p0: Math.floor(status.elapsed_seconds) })}</span>
      {status.rate != null && <span>{t('{p0} в секунду', { p0: status.rate.toLocaleString(uiLocale(), { maximumFractionDigits: 1 }) })}</span>}
      {status.eta_seconds != null && <span>{t('До конца этапа: около {p0} с', { p0: Math.ceil(status.eta_seconds) })}</span>}
      {status.queue_position != null && <span>{t('Место в очереди: {p0}', { p0: status.queue_position })}</span>}
    </div>}
    <RetrievalSummary info={status} />
  </section>;
}
