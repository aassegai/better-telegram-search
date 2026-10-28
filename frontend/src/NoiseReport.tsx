import { t } from './i18n';
import { noiseLabel, type NoiseDecision, type NoiseForm, type NoisePage } from './noiseTypes';

const decisions: Record<string, string> = { keep: 'Сохранить', review: 'Сохранить · неоднозначно', omit: 'Исключить из смыслового текста' };
const reasons: Record<string, string> = { protected_only: 'Защищённые употребления', approved_seed: 'Ранее подтверждённая форма', lexicon_form: 'Есть в частотном справочнике', short_new_form: 'Короткая новая форма', no_noise_hypothesis: 'Нет шумовой гипотезы', candidate_limit: 'Не все гипотезы просмотрены', uncalibrated: 'Неоднозначно; сохраняется' };
const protections: Record<string, string> = { attachment: 'Подпись к вложению', structured_entity: 'Код, ссылка или упоминание', question_reply: 'Ответ на вопрос', uppercase: 'Заглавные буквы', titlecase: 'Начальная заглавная', number: 'Число', alphanumeric: 'Буквы и цифры', mixed_scripts: 'Разные письменности', compound_term: 'Составной термин' };
const operations: Record<string, string> = { exact: 'Без правки', unchanged: 'Без изменения', insertion: 'Лишний символ', deletion: 'Пропущенный символ', substitution: 'Замена символа', transposition: 'Перестановка соседних символов', lengthening: 'Растягивание букв', repeated_block: 'Повтор блока', laughter_pattern: 'Чередующийся смех' };
const score = (value: number | null) => value !== null && Number.isFinite(value) ? value.toFixed(2) : '—';

function Evidence({ row }: { row: NoiseForm }) {
  const candidate = row.candidate;
  return <details className="noise-evidence">
    <summary>{t('Почему такое решение')}</summary>
    <p>{t(noiseLabel(reasons, candidate?.reason, 'Нет данных'))}</p>
    {Object.entries(row.protections).map(([kind, count]) => <p key={kind}>{t(noiseLabel(protections, kind, 'Защищённое употребление'))}: {count}</p>)}
    {candidate && <>
      <p className="baseline-note">{t('Оценки не являются вероятностями. Новые формы автоматически не исключаются.')}</p>
      <p>{t('Разница оценок')}: {score(candidate.margin)}</p>
      <p><strong>{t('Без изменения')}</strong>: <bdi>{candidate.unchanged.form}</bdi> · {score(candidate.unchanged.score)}</p>
      {(['noise', 'useful'] as const).map(kind => <div key={kind}>
        <strong>{t(kind === 'noise' ? 'Шумовые гипотезы' : 'Полезные гипотезы')}</strong>
        {candidate[kind].length === 0 && <p>{t('Нет гипотез')}</p>}
        <ul>{candidate[kind].map(item => <li key={item.form}><bdi>{item.form}</bdi> · {t(noiseLabel(operations, item.operation, 'Гипотеза'))} · {t('Частота: {p0}', { p0: item.corpus_count })} · {score(item.score)}</li>)}</ul>
      </div>)}
      {!candidate.complete && <p className="warning">{t('Список гипотез ограничен. Новые формы сохраняются.')}</p>}
    </>}
  </details>;
}

export default function NoiseReport({ page, overrides, disabled, onOverride }: {
  page: NoisePage; overrides: Record<string, NoiseDecision>; disabled: boolean;
  onOverride: (form: string, value: NoiseDecision | '') => void;
}) {
  return <div className="noise-report">
    {page.forms?.map(row => {
      const effective = Object.hasOwn(overrides, row.form) ? overrides[row.form] : row.default_decision ?? (row.candidate?.decision === 'omit' ? 'omit' : 'keep');
      return <article className="noise-form" key={row.form}>
      <div className="noise-form-heading"><strong><bdi>{row.form}</bdi></strong><span>{t('Решение анализа: {p0}', { p0: t(noiseLabel(decisions, row.candidate?.decision, 'Не рассмотрено')) })}</span></div>
      <p className="noise-effective">{t('Правило проекта: {p0}', { p0: t(effective === 'omit' ? 'Исключать с учётом защит' : 'Сохранять') })}</p>
      {row.inherited_decision && <p className="baseline-note">{t('Ранее выбранное правило: {p0}', { p0: t(row.inherited_decision === 'omit' ? 'Исключать с учётом защит' : 'Сохранять') })}</p>}
      <p>{t('Сообщений: {p0}', { p0: row.whole_count })} · {t('Допустимо к фильтрации: {p0}', { p0: row.eligible_count })}</p>
      <p>{t('Дней: {p0} · авторов: {p1} · защищено: {p2}', { p0: row.days, p1: row.authors, p2: Math.max(0, row.whole_count - row.eligible_count) })}</p>
      <label>{t('Решение для {p0}', { p0: row.form })}<select disabled={disabled} value={Object.hasOwn(overrides, row.form) ? overrides[row.form] : ''} onChange={event => onOverride(row.form, event.target.value as NoiseDecision | '')}>
        <option value="">{t('Без нового переопределения')}</option><option value="keep">{t('Всегда сохранять')}</option><option value="omit">{t('Исключать с учётом защит')}</option>
      </select></label>
      <Evidence row={row} />
    </article>; })}
    {page.families && <>
      <p className="baseline-note">{t('Статистика семейства не означает исключение всех вариантов.')}</p>
      {page.families.map(row => <article className="noise-form" key={row.seed}>
        <strong><bdi>{row.seed}</bdi></strong>
        <p>{t('Вхождений: {p0} · допустимо: {p1}', { p0: row.observed_count, p1: row.eligible_count })}</p>
        <p>{t('Исключить: {p0} · неоднозначно: {p1} · сохранить: {p2}', { p0: row.omit_count, p1: row.review_count, p2: row.keep_count })}</p>
      </article>)}
    </>}
    {(page.forms ?? page.families ?? []).length === 0 && <p>{t('В этой части отчёта нет записей.')}</p>}
  </div>;
}
