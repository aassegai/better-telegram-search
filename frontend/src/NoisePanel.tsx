import { useEffect, useRef, useState } from 'react';
import { api } from './api';
import { t, uiLocale } from './i18n';
import { useDialogOperation } from './useDialogOperation';
import NoiseReport from './NoiseReport';
import NoiseComparison from './NoiseComparison';
import { noiseActive, noiseApplicable, noiseLabel, type NoiseDecision, type NoiseDraft, type NoiseJob, type NoisePage, type NoisePreview } from './noiseTypes';

const states: Record<string, string> = { queued: 'В очереди', running: 'Анализируем', cancelling: 'Останавливаем…', cancelled: 'Отменён', ready: 'Отчёт готов', limited: 'Неполный отчёт', failed: 'Ошибка анализа', stale: 'Анализ устарел', interrupted: 'Прерван перезапуском' };
const phases: Record<string, string> = { queued: 'Ожидаем очередь', scan: 'Считаем сообщения', lexicon: 'Готовим словарь гипотез', candidates: 'Сравниваем варианты', failed: 'Ошибка анализа' };
const errors: Record<string, string> = { source_changed: 'Диалог или исключения изменились. Запустите анализ заново.', time_limit: 'Достигнут лимит времени анализа.', report_size_limit: 'Достигнут лимит размера отчёта.', analysis_failed: 'Не удалось выполнить анализ. Запустите его заново.', result_save_failed: 'Не удалось сохранить анализ. Перезапустите приложение.', interrupted: 'Анализ прерван перезапуском.' };
const limits: Record<string, string> = { message_limit: 'Лимит сообщений', form_limit: 'Лимит однословных форм', phrase_form_limit: 'Лимит слов из фраз', text_limit: 'Ограничение размера сообщения', candidate_limit: 'Ограничение гипотез', payload_limit: 'Ограничение подробностей отчёта' };
type View = 'forms' | 'families';
type Action = (current: () => boolean, signal: AbortSignal) => Promise<void>;

export default function NoisePanel({ chatId, currentPolicyId, initiallyOpen, onOpenChange, pending, onStart, onEnd }: {
  chatId: string; currentPolicyId: string; initiallyOpen: boolean; onOpenChange: (value: boolean) => void; pending: boolean; onStart: () => void; onEnd: () => void;
}) {
  const base = `/api/chats/${encodeURIComponent(chatId)}/noise-analyses`;
  const [open, setOpen] = useState(initiallyOpen);
  const [jobs, setJobs] = useState<NoiseJob[]>([]);
  const [job, setJob] = useState<NoiseJob | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const selectedRef = useRef<string | null>(null);
  const [draft, setDraft] = useState<NoiseDraft | null>(null);
  const [overrides, setOverrides] = useState<Record<string, NoiseDecision>>({});
  const [dirty, setDirty] = useState(false);
  const [comparisonProtected, setComparisonProtected] = useState(false);
  const [page, setPage] = useState<NoisePage | null>(null);
  const [view, setView] = useState<View>('forms');
  const [decision, setDecision] = useState('review');
  const [offset, setOffset] = useState(0);
  const [preview, setPreview] = useState<NoisePreview | null>(null);
  const [notice, setNotice] = useState('');
  const [pollEpoch, setPollEpoch] = useState(0);
  const [initialized, setInitialized] = useState(false);
  const [reloadRequested, setReloadRequested] = useState(false);
  const [maxMessages, setMaxMessages] = useState(1000000);
  const [timeLimit, setTimeLimit] = useState(300);
  const request = useRef<AbortController | null>(null);
  const interaction = useRef(0);
  const op = useDialogOperation();

  useEffect(() => () => request.current?.abort(), []);
  const clearReview = () => { setPage(null); setPreview(null); setOverrides({}); setDirty(false); setDraft(null); setOffset(0); };
  const choose = (id: string | null) => {
    if (id !== selectedRef.current) { clearReview(); setNotice(''); }
    selectedRef.current = id; setSelected(id);
  };
  const acceptJob = (value: NoiseJob) => {
    setJob(value); setJobs(previous => previous.map(item => item.id === value.id ? value : item));
    if (value.stale) {
      setPage(null); setPreview(null); setOverrides({}); setDirty(false);
      setDraft(previous => previous ? { ...previous, overrides: {}, overrides_available: false, stale: true } : null);
    }
  };
  const acceptDraft = (value: NoiseDraft, preserveEdits = false) => {
    if (preview && (preview.revision !== value.revision || (draft && (draft.current_policy_id !== value.current_policy_id || JSON.stringify(draft.overrides) !== JSON.stringify(value.overrides))))) setPreview(null);
    if (value.stale || !value.overrides_available) {
      setDraft(value); setOverrides({}); setDirty(false); setPage(null); setPreview(null); return;
    }
    if (preserveEdits) {
      if (draft && value.revision !== draft.revision) op.setError(t('Проект изменился. Обновите его перед сохранением.'));
      return;
    }
    setDraft(value); setOverrides(value.overrides); setDirty(false);
  };
  const perform = (action: Action, mutation = false) => void op.run(async current => {
    const generation = interaction.current;
    const valid = () => current() && generation === interaction.current;
    const controller = new AbortController(); request.current = controller;
    if (mutation) onStart();
    try { await action(valid, controller.signal); }
    catch (error) { if (!controller.signal.aborted) throw error; }
    finally {
      if (mutation) onEnd();
      if (request.current === controller) request.current = null;
      if (valid()) setPollEpoch(value => value + 1);
    }
  });
  const loadList: Action = async (current, signal) => {
    const value = await api<{ analyses: NoiseJob[] }>(base, { signal });
    if (!current()) return;
    setJobs(value.analyses); setInitialized(true);
    const chosen = value.analyses.find(item => item.id === selectedRef.current) ?? value.analyses[0] ?? null;
    choose(chosen?.id ?? null); setJob(chosen);
    if (chosen) {
      acceptJob(chosen);
      const metadata = await api<NoiseDraft>(`${base}/${chosen.id}/draft`, { signal });
      if (current() && selectedRef.current === chosen.id) acceptDraft(metadata, dirty);
    }
  };
  useEffect(() => { if (initiallyOpen) perform(loadList); }, []);
  useEffect(() => {
    if (reloadRequested && open && !op.busy) { setReloadRequested(false); perform(loadList); }
  }, [reloadRequested, open, op.busy]);
  useEffect(() => {
    if (!open || !selected) return;
    let alive = true;
    let timer: ReturnType<typeof setTimeout>;
    const controller = new AbortController();
    const valid = op.guard();
    const poll = async () => {
      if (!alive || !valid()) return;
      try {
        const value = await api<NoiseJob>(`${base}/${selected}`, { signal: controller.signal });
        if (alive && valid() && selectedRef.current === selected) acceptJob(value);
      } catch (error) {
        if (alive && valid()) op.setError(error instanceof Error ? error.message : t('Ошибка соединения.'));
      }
      if (alive && valid()) timer = setTimeout(() => void poll(), 2000);
    };
    void poll();
    return () => { alive = false; clearTimeout(timer); controller.abort(); };
  }, [open, selected, pollEpoch, base]);
  useEffect(() => {
    // External policy/model rebuilds make any earlier preview inapplicable.
    setPreview(null); setPage(null); setPollEpoch(value => value + 1);
  }, [currentPolicyId]);

  const loadReport = (nextView = view, nextDecision = decision, nextOffset = offset) => perform(async (current, signal) => {
    if (!selected) return;
    setPage(null);
    const query = new URLSearchParams({ offset: String(nextOffset), limit: '50' });
    if (nextView === 'forms' && nextDecision) query.set('decision', nextDecision);
    const [result, metadata] = await Promise.all([
      api<NoisePage>(`${base}/${selected}/${nextView}?${query}`, { signal }),
      api<NoiseDraft>(`${base}/${selected}/draft`, { signal }),
    ]);
    if (!current() || selectedRef.current !== selected) return;
    if (metadata.stale) { acceptDraft(metadata); return; }
    setPage(result); setView(nextView); setDecision(nextDecision); setOffset(nextOffset); acceptDraft(metadata, dirty);
  });
  const start = () => perform(async (current, signal) => {
    const value = await api<NoiseJob>(base, { method: 'POST', signal, body: JSON.stringify({ max_messages: maxMessages, time_limit_seconds: timeLimit }) });
    if (current()) { choose(value.id); clearReview(); setJobs(previous => [value, ...previous.filter(item => item.id !== value.id)]); setJob(value); setNotice(''); }
  }, true);
  const cancel = () => perform(async (current, signal) => {
    if (!selected) return;
    const value = await api<NoiseJob>(`${base}/${selected}/cancel`, { method: 'POST', signal });
    if (current()) acceptJob(value);
  }, true);
  const remove = () => perform(async (current, signal) => {
    if (!selected) return;
    await api(`${base}/${selected}`, { method: 'DELETE', signal });
    if (current()) { choose(null); setJob(null); clearReview(); await loadList(current, signal); }
  }, true);
  const changeOverride = (form: string, value: NoiseDecision | '') => {
    if (value && !Object.hasOwn(overrides, form) && Object.keys(overrides).length >= 1000) {
      op.setError(t('Допускается не более 1000 переопределений в одном отчёте.')); return;
    }
    const next = { ...overrides }; if (value) next[form] = value; else delete next[form];
    setOverrides(next);
    const canonical = (values: Record<string, NoiseDecision>) => JSON.stringify(Object.entries(values).sort(([left], [right]) => left.localeCompare(right)));
    setDirty(canonical(next) !== canonical(draft?.overrides ?? {})); setPreview(null); setNotice('');
  };
  const save = () => perform(async (current, signal) => {
    if (!selected || !draft) return;
    const value = await api<NoiseDraft>(`${base}/${selected}/draft`, { method: 'PATCH', signal, body: JSON.stringify({ revision: draft.revision, overrides }) });
    if (current()) { acceptDraft(value); setPreview(null); setNotice(t('Переопределения сохранены.')); }
  }, true);
  const discard = () => perform(async (current, signal) => {
    if (!selected) return;
    const value = await api<NoiseDraft>(`${base}/${selected}/draft`, { signal });
    if (current()) { acceptDraft(value); setPreview(null); }
  });
  const inspect = () => perform(async (current, signal) => {
    if (!selected || !draft) return;
    const value = await api<NoisePreview>(`${base}/${selected}/preview`, { method: 'POST', signal, body: JSON.stringify({ revision: draft.revision }) });
    if (current()) setPreview(value);
  });
  const apply = () => perform(async (current, signal) => {
    if (!selected || !draft || !preview) return;
    if (preview.revision !== draft.revision) { setPreview(null); op.setError(t('Проект изменился. Обновите его перед сохранением.')); return; }
    const result = await api<{ policy_id: string; already_applied: boolean }>(`${base}/${selected}/apply`, { method: 'POST', signal, body: JSON.stringify({ revision: preview.revision }) });
    if (!current()) return;
    setPreview(null); setNotice(t(result.already_applied ? 'Эти правила уже используются. Повторное перестроение не запущено.' : 'Правила выбраны. Текстовый индекс будет перестроен с сохранением паузы.'));
    const metadata = await api<NoiseDraft>(`${base}/${selected}/draft`, { signal });
    if (current()) acceptDraft(metadata);
  }, true);

  const busy = pending || op.busy;
  const active = noiseActive(job);
  const canReview = !!job && !job.stale && ['ready', 'limited'].includes(job.state) && !active;
  const hasDigest = /^[a-f0-9]{64}$/u.test(job?.summary?.report_sha256 ?? '');
  const canEdit = canReview && job?.state === 'ready' && noiseApplicable(job.summary) && hasDigest && !!draft?.overrides_available && !draft.stale;
  const canApply = canEdit && !dirty;
  const validOptions = Number.isInteger(maxMessages) && maxMessages >= 1 && maxMessages <= 1000000 && Number.isInteger(timeLimit) && timeLimit >= 1 && timeLimit <= 3600;
  const applied = !!draft?.applied_policy_id && draft.applied_policy_id === currentPolicyId;
  return <section className="index-card noise-panel" aria-label={t('Фильтр смыслового шума')} aria-busy={busy}>
    <details open={open} onToggle={event => {
      if (event.target !== event.currentTarget) return;
      const visible = event.currentTarget.open; setOpen(visible); onOpenChange(visible);
      if (visible) { if (!op.busy) perform(loadList); else setReloadRequested(true); }
      else { interaction.current++; request.current?.abort(); setReloadRequested(false); setPage(null); setPreview(null); }
    }}>
      <summary>{t('Фильтр смыслового шума')}</summary>
      <p>{t('Локальный анализ однословных реакций и опечаток. Исходная переписка и поиск по словам сохраняются.')}</p>
      <p className="baseline-note">{t('Автоматический отсев новых форм пока выключен. Спорные формы сохраняются; исключение можно задать вручную.')}</p>
      <details className="noise-limits"><summary>{t('Лимиты анализа')}</summary>
        <label>{t('Максимум сообщений')}<input type="number" min="1" max="1000000" value={Number.isNaN(maxMessages) ? '' : maxMessages} disabled={busy} onChange={event => setMaxMessages(event.target.value === '' ? NaN : Number(event.target.value))} /></label>
        <label>{t('Лимит времени, с')}<input type="number" min="1" max="3600" value={Number.isNaN(timeLimit) ? '' : timeLimit} disabled={busy} onChange={event => setTimeLimit(event.target.value === '' ? NaN : Number(event.target.value))} /></label>
      </details>
      <div className="job-actions">
        <button type="button" className="primary" disabled={busy || !initialized || dirty || !validOptions || jobs.length >= 8 || jobs.some(noiseActive)} onClick={start}>{t('Запустить анализ')}</button>
        <button type="button" disabled={busy || dirty} onClick={() => perform(loadList)}>{t('Обновить анализы')}</button>
      </div>
      {jobs.length >= 8 && <p className="baseline-note">{t('Сохранено восемь отчётов. Удалите ненужный перед новым анализом.')}</p>}
      {jobs.length > 0 && <label>{t('Отчёт анализа')}<select disabled={busy || dirty} value={selected ?? ''} onChange={event => {
        const id = event.target.value; choose(id); const chosen = jobs.find(item => item.id === id); if (chosen) acceptJob(chosen);
        perform(async (current, signal) => { const metadata = await api<NoiseDraft>(`${base}/${id}/draft`, { signal }); if (current() && selectedRef.current === id) acceptDraft(metadata); });
      }}>{jobs.map(item => <option key={item.id} value={item.id}>{new Date(item.created_at * 1000).toLocaleString(uiLocale())} · {t(noiseLabel(states, item.state, 'Нет данных'))} · {item.id.slice(0, 8)}</option>)}</select></label>}
      {job && <div className="noise-job" aria-live="polite">
        <strong>{t(noiseLabel(states, job.state, 'Нет данных'))}</strong>
        {active && <>
          <p>{t(noiseLabel(phases, job.progress.phase, 'Анализируем'))} · {t('Прошло {p0} с', { p0: Math.round(job.progress.elapsed_seconds) })}</p>
          <progress aria-label={t('Прогресс анализа')} value={job.progress.total !== null ? job.progress.completed : undefined} max={job.progress.total || 1} />
          {job.progress.waiting_reason === 'database' && <p>{t('Ожидаем доступ к базе.')}</p>}
          <button type="button" disabled={busy || job.state === 'cancelling'} onClick={cancel}>{t('Отменить анализ')}</button>
        </>}
        <p>{t('Просмотрено сообщений: {p0} · однословных: {p1} · форм: {p2}', { p0: job.summary?.scanned_messages ?? job.progress.scanned_messages ?? 0, p1: job.summary?.whole_messages ?? job.progress.whole_messages ?? 0, p2: job.summary?.forms ?? job.progress.forms ?? 0 })}</p>
        {job.stale && <p className="warning">{t('Диалог или исключения изменились. Запустите анализ заново.')}</p>}
        {job.error && !job.stale && <p className="error" role="alert">{t(noiseLabel(errors, job.error, 'Не удалось выполнить анализ. Запустите его заново.'))}</p>}
        {job.summary?.limits?.map(limit => <p className="warning" key={limit}>{t(noiseLabel(limits, limit, 'Неполное покрытие анализа'))}</p>)}
        {job.state === 'ready' && noiseApplicable(job.summary) && job.summary?.evidence_complete === false && <p className="baseline-note">{t('Все сообщения области обработаны. Список гипотез ограничен; спорные формы сохраняются. Можно применить точные правила.')}</p>}
        {job.state === 'limited' && <p className="baseline-note">{t('Неполный отчёт можно просматривать, но нельзя применить. Увеличьте лимиты и запустите новый анализ.')}</p>}
        {job.state === 'ready' && (!hasDigest || !noiseApplicable(job.summary)) && <p className="baseline-note">{t('Для применения этого отчёта нужен новый анализ.')}</p>}
        <div className="job-actions"><button type="button" disabled={busy || !canReview} onClick={() => loadReport(view, decision, 0)}>{t('Просмотреть отчёт')}</button><button type="button" disabled={busy || active || comparisonProtected || dirty} onClick={remove}>{t('Удалить отчёт')}</button></div>
        <p className="baseline-note">{t('Удаление отчёта не меняет уже выбранные правила и индексы.')}</p>
      </div>}
      {page && <>
        <div className="noise-report-controls">
          <label>{t('Часть отчёта')}<select value={view} disabled={busy} onChange={event => loadReport(event.target.value as View, decision, 0)}><option value="forms">{t('Формы')}</option><option value="families">{t('Семейства')}</option></select></label>
          {view === 'forms' && <label>{t('Решения анализа')}<select disabled={busy} value={decision} onChange={event => loadReport(view, event.target.value, 0)}><option value="">{t('Все решения')}</option><option value="review">{t('Неоднозначные')}</option><option value="keep">{t('Сохранить')}</option><option value="omit">{t('Исключить из смыслового текста')}</option></select></label>}
        </div>
        <NoiseReport page={page} overrides={overrides} disabled={busy || !canEdit} onOverride={changeOverride} />
        <div className="job-actions"><button type="button" disabled={busy || offset === 0} onClick={() => loadReport(view, decision, Math.max(0, offset - 50))}>{t('Предыдущие записи')}</button><button type="button" disabled={busy || page.next_offset === null} onClick={() => { if (page.next_offset !== null) loadReport(view, decision, page.next_offset); }}>{t('Следующие записи')}</button></div>
      </>}
      {draft && !draft.stale && <div className="noise-draft">
        <p>{t('Ручных правил: {p0} / 1000', { p0: Object.keys(overrides).length })}</p>
        {dirty && <p className="baseline-note">{t('Есть несохранённые правки. Они ещё не меняют индекс.')}</p>}
        <div className="job-actions"><button type="button" disabled={busy || !canEdit || !dirty || Object.keys(overrides).length > 1000} onClick={save}>{t('Сохранить переопределения')}</button><button type="button" disabled={busy || !dirty} onClick={discard}>{t('Отменить правки')}</button><button type="button" disabled={busy || !canApply} onClick={inspect}>{t('Предпросмотр правил')}</button></div>
      </div>}
      {preview && <div className="noise-preview">
        <p>{t('Дополнительных точных исключений: {p0} · правил сохранения: {p1}', { p0: preview.explicit_omit_forms, p1: preview.keep_forms })}</p>
        <p className="baseline-note">{t('Итог включает унаследованные правила выбранной политики.')}</p>
        <p className="baseline-note">{t('Применение перестроит только текст этого диалога. Паузы, устройства, изображения и OCR сохраняются.')}</p>
        <details><summary>{t('ID политики')}</summary><code>{preview.policy_id}</code></details>
        <button type="button" className="primary" disabled={busy || !canApply || preview.revision !== draft?.revision} onClick={apply}>{t('Применить правила и перестроить текст')}</button>
      </div>}
      {open && selected && draft && <NoiseComparison key={`${selected}:${draft.revision}`} base={`${base}/${selected}`} revision={draft.revision} canStart={canApply} parentBusy={busy} onProtectionChange={setComparisonProtected} />}
      {applied && <p role="status">{t('Эти правила выбраны для диалога.')}</p>}
      {notice && <p role="status">{t(notice)}</p>}
      {op.busy && <p role="status">{t('Выполняем операцию…')}</p>}
      {op.error && <p className="error" role="alert">{t(op.error)}</p>}
    </details>
  </section>;
}
