import { getLanguage, t, uiLocale } from './i18n';
import { forwardRef, useEffect, useImperativeHandle, useLayoutEffect, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import { api, ApiError } from './api';
import SearchTabPersistence from './SearchTabPersistence';
import { recoverTabJournal } from './SearchTabJournal';
import ForwardFilter from './ForwardFilter';
import SearchSettingsPanel from './SearchSettingsPanel';
import type { SearchSettings } from './SearchSettingsPanel';
import FavoriteButton from './FavoriteButton';
import SearchProgress from './SearchProgress';
import { RetrievalChoice, RetrievalSummary } from './RetrievalStrategy';
import Icon from './Icon';
import DateField from './DateField';
import ImageViewer from './ImageViewer';
import type { OpenImage } from './ImageViewer';
import type { Chat, Hit, MediaStatus, Message, RestoredSearch, RetrievalInfo, SearchTab, TabResult, SearchIntent, SearchPage, SearchJobStatus, SearchModality, SemanticStatus } from './types';

export type SearchPaneHandle = {
  setScope: (scope: string[] | null) => void;
  restore: (value: RestoredSearch, repeat: boolean) => void;
  removedChat: (id: string) => void;
  flush: () => Promise<void>;
  toggleChat: (id: string) => void;
};
export type SearchPaneProps = {
  tab: SearchTab; active: boolean; activation: number; transitioning: boolean; chats: Chat[]; chatsLoaded: boolean; connected: boolean;
  authorsVersion: string; semantic: SemanticStatus | null; media: MediaStatus | null;
  onScope: (scope: string[] | null) => void;
  onLabel: (query: string, mode: string, busy: boolean) => void;
  onMissing: () => void;
};
const dates = {
  ru: new Intl.DateTimeFormat('ru-RU', { dateStyle: 'medium', timeStyle: 'short' }),
  en: new Intl.DateTimeFormat('en-US', { dateStyle: 'medium', timeStyle: 'short' }),
};
const date = (value: number) => dates[getLanguage()].format(new Date(value * 1000));
const normalize = (text: string) => text.normalize('NFKC').toLocaleLowerCase('ru').replaceAll('ё', 'е');
const reasons: Record<string, string> = { words: 'Совпали слова', meaning: 'Близкий смысл', image: 'Фотография по описанию', ocr_words: 'Слова на фотографии', ocr_meaning: 'Смысл текста на фотографии' };

function searchParameters(spec: SearchIntent) {
  const params = new URLSearchParams({ q: spec.query, exact: String(spec.exact_phrase),
    content_type: spec.content_type, mode: spec.mode, exclude_deleted: String(spec.exclude_deleted) });
  spec.modalities.forEach(kind => params.append('modality', kind));
  spec.scope.chat_ids?.forEach(id => params.append('chat_id', id));
  spec.author_ids.forEach(id => params.append('author_id', id));
  spec.forwarded_from.forEach(name => params.append('forwarded_from', name));
  if (spec.date_from) params.set('date_from', spec.date_from);
  if (spec.date_to) params.set('date_to', spec.date_to);
  return params.toString();
}

const allModalities: SearchModality[] = ['text', 'images', 'ocr'];
const modalityLabels: Record<SearchModality, string> = { text: 'Текст', images: 'Изображения', ocr: 'OCR' };

function Highlight({ text, query }: { text: string; query: string }) {
  const words = new Set(normalize(query).match(/[\p{L}\p{N}]+/gu) || []);
  return <>{text.split(/([\p{L}\p{N}]+)/u).map((part, i) =>
    words.has(normalize(part)) ? <mark key={i}>{part}</mark> : part)}</>;
}

function MessageRow({ message, anchor, query = '', onOpenImage }: { message: Message; anchor: number; query?: string; onOpenImage: (image: OpenImage) => void }) {
  const photo = message.media.find(media => media.kind === 'photo');
  const [failed, setFailed] = useState(false);
  return <div className={`message ${message.message_id === anchor ? 'anchor' : ''}`}>
    <div className="message-meta"><strong>{message.author || t('Служебное событие')}</strong>
      <time>{date(message.timestamp)}</time>
      {!message.matches_filters && <span className="context-tag">{t("вне фильтра · контекст")}</span>}
    </div>
    {!!message.remote_deleted && <div className="message-note remote-deleted">{t('Удалено в Telegram · архивная копия')}</div>}
    {message.forwarded_from && <div className="message-note">{t("Переслано: ")}{message.forwarded_from}</div>}
    {message.reply_to && <div className="message-note">{t("Ответ на #")}{message.reply_to}</div>}
    <div className="message-text"><Highlight text={message.text || message.action || t('Сообщение без текста')} query={query} /></div>
    {photo && (photo.status === 'ready' && !failed ?
      <button type="button" className="photo-link" aria-label={t('Открыть изображение')} onClick={() => onOpenImage({ id: photo.id, messageId: message.message_id })}>
        <img loading="lazy" src={`/api/media/${photo.id}`} alt={t("Фотография из сообщения")} onError={() => setFailed(true)} />
      </button> : <div className="missing-photo">{t("Изображение недоступно в папке источника")}</div>)}
    {message.media.some(media => media.kind === 'attachment') && <div className="message-note">{t("В исходном экспорте есть вложение")}</div>}
  </div>;
}

const SearchPane = forwardRef<SearchPaneHandle, SearchPaneProps>(function SearchPane({
  tab, active, activation, transitioning, chats, chatsLoaded, connected, authorsVersion, semantic, media, onScope, onLabel, onMissing,
}, ref) {
  const [recovered] = useState(() => recoverTabJournal(tab));
  const initial = recovered?.draft ?? tab.draft;
  const initialView = recovered?.view ?? tab.view;
  const [selected, setSelected] = useState<string[] | null>(initial.scope.mode === 'all' ? null : [...(initial.scope.chat_ids ?? [])]);
  const [authors, setAuthors] = useState<{ author_id: string; name: string }[]>([]);
  const [query, setQuery] = useState(initial.query);
  const [submitted, setSubmitted] = useState('');
  const [authorIds, setAuthorIds] = useState<string[]>(initial.author_ids);
  const author = authorIds.length > 1 ? '__multiple__' : authorIds[0] ?? '';
  const setAuthor = (value: string) => setAuthorIds(value ? [value] : []);
  const [forwardedFrom, setForwardedFrom] = useState<string[]>(initial.forwarded_from);
  const [from, setFrom] = useState(initial.date_from ?? '');
  const [to, setTo] = useState(initial.date_to ?? '');
  const [contentType, setContentType] = useState(initial.content_type);
  const [exact, setExact] = useState(initial.exact_phrase);
  const [excludeDeleted, setExcludeDeleted] = useState(initial.exclude_deleted);
  const [mode, setMode] = useState(initial.mode);
  const [effectiveMode, setEffectiveMode] = useState('words');
  const [retrieval, setRetrieval] = useState<RetrievalInfo | null>(null);
  const [warnings, setWarnings] = useState<string[]>([]);
  const [reranked, setReranked] = useState(false);
  const [modalities, setModalities] = useState<SearchModality[]>(initial.modalities);
  const [submittedModalities, setSubmittedModalities] = useState<SearchModality[]>(allModalities);
  const [hits, setHits] = useState<Hit[] | null>(null);
  const [hasMore, setHasMore] = useState(false);
  const [submittedLimit, setSubmittedLimit] = useState(20);
  const [searchId, setSearchId] = useState<string | null>(null);
  const [nextOffset, setNextOffset] = useState<number | null>(null);
  const [browseCursor, setBrowseCursor] = useState<string | null>(null);
  const [searchJob, setSearchJob] = useState<SearchJobStatus | null>(null);
  const [searchStopped, setSearchStopped] = useState(tab.last_state === 'cancelled');
  const [loadingMore, setLoadingMore] = useState(false);
  const [pagingError, setPagingError] = useState('');
  const searchVersion = useRef(0);
  const activeSearch = useRef<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [showSearchSettings, setShowSearchSettings] = useState(initialView.display_settings_open);
  const [searchPreferences, setSearchPreferences] = useState<SearchSettings | null>({ search_result_limit: initial.page_size ?? 20, display_chunk_size: initial.chunk_size ?? 10 });
  const [preferencesVersion, setPreferencesVersion] = useState(0);
  const [strategy, setStrategy] = useState<'exact' | 'ann'>(initial.strategy ?? 'exact');
  const [sort, setSort] = useState<'newest' | 'oldest'>(initial.sort ?? 'newest');
  const [savedHistoryId, setSavedHistoryId] = useState<string | null>(null);
  const [savedSnapshotId, setSavedSnapshotId] = useState<string | null>(null);
  const acceptedSnapshot = useRef<string | null>(null);
  const displayVersion = useRef(0);
  const [historyPaging, setHistoryPaging] = useState<string | null>(null);
  const [favorite, setFavorite] = useState(false);
  const [favoriteBusy, setFavoriteBusy] = useState(false);
  const [submittedIntent, setSubmittedIntent] = useState<SearchIntent | null>(null);
  const [snapshotNotice, setSnapshotNotice] = useState(tab.interrupted ? 'Предыдущий поиск прерван. Запустите его снова, когда будете готовы.' : '');
  const [error, setError] = useState(tab.error ?? '');
  const [context, setContext] = useState<{ hit: Hit; messages: Message[] } | null>(null);
  const [openImage, setOpenImage] = useState<OpenImage | null>(null);
  const [loadingContext, setLoadingContext] = useState(false);
  const contextVersion = useRef(0);
  const [searchFilters, setSearchFilters] = useState('');
  const closeContext = () => { contextVersion.current++; setContext(null); setLoadingContext(false); };
  const reportError = (error: unknown) => setError(error instanceof Error ? error.message : t('Ошибка соединения.'));
  const persistenceRef = useRef<SearchTabPersistence | null>(null);
  if (!persistenceRef.current) persistenceRef.current = new SearchTabPersistence(tab, reportError, onMissing);
  const persistence = persistenceRef.current;
  const content = useRef<HTMLDivElement>(null);
  const scroll = useRef(initialView.scroll_y);
  const [checkedActivation, setCheckedActivation] = useState(-1);
  const restoredOnce = useRef(false);
  const activeRef = useRef(active);
  activeRef.current = active;
  const activationRef = useRef(activation);
  activationRef.current = activation;
  const draft: SearchIntent = { query,
    scope: selected === null ? { mode: 'all' } : { mode: 'selected', chat_ids: selected },
    author_ids: authorIds, forwarded_from: forwardedFrom, date_from: from || null, date_to: to || null,
    content_type: contentType, exclude_deleted: excludeDeleted, exact_phrase: exact, mode,
    modalities, strategy, sort, page_size: searchPreferences?.search_result_limit ?? 20,
    chunk_size: searchPreferences?.display_chunk_size ?? 10 };
  const draftRef = useRef(draft);
  draftRef.current = draft;
  const setScope = (scope: string[] | null) => {
    setSelected(scope);
    if (activeRef.current) onScope(scope);
  };
  const hitsCount = useRef(0);
  hitsCount.current = hits?.length ?? 0;
  const settingsOpen = useRef(showSearchSettings);
  settingsOpen.current = showSearchSettings;
  const saveView = () => persistence.schedule(draftRef.current,
    { visible_count: hitsCount.current, scroll_y: scroll.current, display_settings_open: settingsOpen.current },
    acceptedSnapshot.current);
  const saveScroll = () => {
    if (!activeRef.current || checkedActivation !== activation) return;
    scroll.current = window.matchMedia('(max-width: 700px)').matches ? window.scrollY : (content.current?.scrollTop ?? 0);
    saveView();
  };
  useEffect(() => {
    if (!restoredOnce.current && tab.snapshot) return;
    saveView();
    onLabel(query, mode, busy);
  }, [JSON.stringify(draft), hits?.length, showSearchSettings, busy]);
  useEffect(() => {
    if (!active) { closeContext(); setOpenImage(null); return; }
    let alive = true;
    const version = searchVersion.current;
    const accepted = acceptedSnapshot.current;
    const restore = async () => {
      const metadata = await api<SearchTab>(`/api/search-tabs/${tab.id}`);
      if (!alive || version !== searchVersion.current) return;
      if (acceptedSnapshot.current !== accepted) return;
      persistence.adopt(metadata);
      if (!restoredOnce.current && metadata.snapshot) {
        const result = await api<TabResult>(`/api/search-tabs/${tab.id}/result?snapshot_id=${encodeURIComponent(metadata.snapshot.snapshot_id)}`);
        if (!alive || version !== searchVersion.current) return;
        if (acceptedSnapshot.current !== accepted) return;
        acceptRestored(result, metadata);
      } else if (acceptedSnapshot.current && (!metadata.snapshot?.cache_available || metadata.snapshot.snapshot_id !== acceptedSnapshot.current)) {
        discardDisplayedSnapshot();
      }
      restoredOnce.current = true;
      setCheckedActivation(activation);
      if (metadata.job_id && !activeSearch.current) void observeExisting(metadata.job_id);
    };
    void restore().catch(error => {
      if (alive && version === searchVersion.current && acceptedSnapshot.current === accepted) {
        if (acceptedSnapshot.current) discardDisplayedSnapshot();
        if (error instanceof ApiError && error.status === 404) onMissing();
        else reportError(error);
        setCheckedActivation(activation);
      }
    });
    return () => { alive = false; };
  }, [active, activation]);
  useLayoutEffect(() => {
    if (!active || checkedActivation !== activation) return;
    if (window.matchMedia('(max-width: 700px)').matches) window.scrollTo(0, scroll.current);
    else if (content.current) content.current.scrollTop = scroll.current;
  }, [active, activation, checkedActivation]);
  useEffect(() => {
    if (!active) return;
    const onScroll = () => saveScroll();
    const onHide = () => { saveView(); void persistence.flush().catch(() => {}); };
    window.addEventListener('scroll', onScroll);
    window.addEventListener('pagehide', onHide);
    return () => { window.removeEventListener('scroll', onScroll); window.removeEventListener('pagehide', onHide); };
  }, [active, activation, checkedActivation]);
  const devices = (values: (string | undefined)[]) => [...new Set(values.filter(Boolean).flatMap(value => value!.toUpperCase().split('+').map(device => device.trim())))].join(' + ') || 'CPU';
  const indexingDevices = devices([semantic?.backend?.device, media?.backend?.device,
    media?.ocr_enabled ? media.ocr_backend?.device : undefined]);
  const searchDevices = devices([semantic?.backend?.query_execution?.device, media?.query_backend?.device]);
  useEffect(() => { if (active) onScope(selected); }, [active, selected]);
  useImperativeHandle(ref, () => ({ setScope, toggleChat: id => {
      const scope = draftRef.current.scope;
      const values = scope.mode === 'all' ? chats.map(chat => chat.id) : scope.chat_ids ?? [];
      setScope(values.includes(id) ? values.filter(value => value !== id) : [...values, id]);
    }, restore: restoreHistory, flush: () => { saveScroll(); saveView(); return persistence.flush(); },
    removedChat: id => {
      const job = activeSearch.current;
      searchVersion.current++; activeSearch.current = null;
      setBusy(false); setSearchJob(null); setSearchStopped(false);
      if (job) void api(`/api/search-jobs/${encodeURIComponent(job)}/cancel`, { method: 'POST' }).catch(() => {});
      setScope(selected?.filter(value => value !== id) ?? null); discardDisplayedSnapshot();
    },
  }));
  useEffect(() => { persistence.resume(); return () => {
    searchVersion.current++; displayVersion.current++; contextVersion.current++;
    if (activeSearch.current) void api(`/api/search-jobs/${encodeURIComponent(activeSearch.current)}/cancel`, { method: 'POST' }).catch(() => {});
    persistence.dispose();
  }; }, []);
  useEffect(() => {
    if (!active) return;
    const params = new URLSearchParams();
    selected?.forEach(id => params.append('chat_id', id));
    let alive = true;
    api<{ author_id: string; name: string }[]>(`/api/authors?${params}`).then(authors => {
      if (alive) {
        setAuthors(authors);
      }
    }).catch(reportError);
    return () => { alive = false; };
  }, [active, selected, chats.length, authorsVersion]);

  useEffect(() => {
    const escape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') { closeContext(); setOpenImage(null); }
    };
    window.addEventListener('keydown', escape);
    return () => window.removeEventListener('keydown', escape);
  }, []);

  useEffect(() => {
    if (!active || !savedSnapshotId || favoriteBusy) return;
    let alive = true, pending = false;
    const controller = new AbortController();
    const snapshot = acceptedSnapshot.current;
    const check = async () => {
      if (pending) return; pending = true;
      try {
        const value = await api<SearchTab>(`/api/search-tabs/${tab.id}`, { signal: controller.signal });
        if (!alive || acceptedSnapshot.current !== snapshot) return;
        persistence.adopt(value);
        if (value.snapshot?.snapshot_id !== snapshot || !value.snapshot.cache_available) discardDisplayedSnapshot();
        else { setFavorite(value.snapshot.favorite); setSavedHistoryId(value.snapshot.id); }
      } catch (error) {
        if (alive && acceptedSnapshot.current === snapshot) {
          if (error instanceof ApiError && error.status === 404) onMissing();
          else reportError(error);
        }
      }
      finally { pending = false; }
    };
    const timer = window.setInterval(() => void check(), 3000);
    window.addEventListener('bts-search-history-changed', check);
    return () => { alive = false; controller.abort(); window.clearInterval(timer); window.removeEventListener('bts-search-history-changed', check); };
  }, [active, savedSnapshotId, favoriteBusy]);

  const changeModalities = (next: SearchModality[]) => {
    setModalities(next);
  };

  async function search(event: FormEvent) {
    event.preventDefault();
    if (busy) return;
    if (query.trim() && !modalities.length) { setError(t('Выберите хотя бы один тип поиска.')); return; }
    await runSearch({ query, scope: selected === null ? { mode: 'all' } : { mode: 'selected', chat_ids: selected },
      author_ids: authorIds, forwarded_from: forwardedFrom, date_from: from || null, date_to: to || null,
      content_type: contentType, exclude_deleted: excludeDeleted, exact_phrase: exact, mode,
      modalities: modalities.length ? modalities : allModalities, strategy, sort,
      ...(searchPreferences ? { page_size: searchPreferences.search_result_limit, chunk_size: searchPreferences.display_chunk_size } : {}),
    });
  }

  function applyIntent(spec: SearchIntent) {
    setQuery(spec.query); setSelected(spec.scope.mode === 'all' ? null : [...(spec.scope.chat_ids ?? [])]);
    setAuthorIds([...spec.author_ids]); setForwardedFrom([...spec.forwarded_from]); setFrom(spec.date_from ?? ''); setTo(spec.date_to ?? '');
    setContentType(spec.content_type); setExcludeDeleted(spec.exclude_deleted); setExact(spec.exact_phrase); setMode(spec.mode);
    setModalities([...spec.modalities]); setStrategy(spec.strategy ?? 'exact'); setSort(spec.sort ?? 'newest');
    setSearchPreferences({ search_result_limit: spec.page_size ?? 20, display_chunk_size: spec.chunk_size ?? 10 });
    setPreferencesVersion(value => value + 1);
  }

  function acceptPage(result: SearchPage, spec: SearchIntent) {
    restoredOnce.current = true;
    displayVersion.current++;
    setSearchId(result.job_id); setNextOffset(result.next_offset ?? null); setBrowseCursor(result.browse_cursor ?? null);
    setHits(result.results); setHasMore(result.has_more); setSubmitted(spec.query); setSubmittedIntent(result.spec ?? spec);
    setSubmittedLimit(result.limit ?? 20); setEffectiveMode(result.effective_mode);
    setRetrieval({ requested_strategy: result.requested_strategy, effective_strategy: result.effective_strategy,
      fallback_reasons: [...(result.fallback_reasons ?? [])] });
    setWarnings(result.warnings ?? []); setReranked(Boolean(result.rerank_applied));
    setSearchFilters(searchParameters(spec)); setSubmittedModalities([...spec.modalities]);
    setSavedHistoryId(result.history_id ?? null); setFavorite(Boolean(result.favorite));
    acceptedSnapshot.current = result.snapshot_id ?? result.job_id;
    setSavedSnapshotId(result.snapshot_id ?? null);
    if (activeRef.current) setCheckedActivation(activationRef.current);
  }

  function discardDisplayedSnapshot() {
    displayVersion.current++; closeContext(); setOpenImage(null);
    setHits(null); setNextOffset(null); setBrowseCursor(null); setLoadingMore(false); setPagingError('');
    setSnapshotNotice('Сохранённая выдача недоступна. Параметры и избранное сохранены; повторите поиск.');
  }

  function showEditorForAction() {
    if (activeRef.current && checkedActivation !== activationRef.current) {
      restoredOnce.current = true;
      discardDisplayedSnapshot(); setSnapshotNotice('');
      setCheckedActivation(activationRef.current);
    }
  }

  function acceptRestored(value: TabResult, metadata: SearchTab) {
    const snapshot = value.entry ?? metadata.snapshot;
    if (!snapshot) return;
    setSubmittedIntent(snapshot.spec); setFavorite(snapshot.favorite); setSavedHistoryId(snapshot.id);
    acceptedSnapshot.current = snapshot.snapshot_id; setSavedSnapshotId(snapshot.snapshot_id);
    if (value.result) {
      acceptPage({ ...value.result, history_id: snapshot.id, favorite: snapshot.favorite }, snapshot.spec);
      setSnapshotNotice('Открыта сохранённая выдача. Новые совпадения появятся после повторного поиска.');
    } else { discardDisplayedSnapshot(); setWarnings([]); }
    setHistoryPaging(tab.id);
  }

  async function observeExisting(jobId: string) {
    const version = ++searchVersion.current;
    activeSearch.current = jobId; setBusy(true); setSearchStopped(false);
    try {
      const job = await api<SearchJobStatus>(`/api/search-jobs/${encodeURIComponent(jobId)}`);
      await observe(job, draftRef.current, version);
    } catch (error) { if (version === searchVersion.current) reportError(error); }
    finally { if (version === searchVersion.current) { setBusy(false); activeSearch.current = null; } }
  }

  async function observe(initialJob: SearchJobStatus, spec: SearchIntent, version: number) {
    let job = initialJob;
    activeSearch.current = job.job_id;
    while (version === searchVersion.current) {
      const snapshot = job;
      setSearchJob(current => current?.job_id === snapshot.job_id && current?.state === 'cancelling' && ['queued', 'running'].includes(snapshot.state) ? current : snapshot);
      if (['completed', 'cancelled', 'failed'].includes(job.state)) break;
      await new Promise(resolve => window.setTimeout(resolve, 350));
      if (version !== searchVersion.current) return;
      job = await api<SearchJobStatus>(`/api/search-jobs/${encodeURIComponent(job.job_id)}`);
    }
    if (version !== searchVersion.current) return;
    if (job.state === 'cancelled') { setSearchStopped(true); return; }
    if (job.state === 'failed') throw new Error(job.error || t('Не удалось выполнить поиск.'));
    const result = await api<SearchPage>(`/api/search-jobs/${encodeURIComponent(job.job_id)}/result`);
    if (version !== searchVersion.current) return;
    const metadata = await api<SearchTab>(`/api/search-tabs/${tab.id}`);
    if (version !== searchVersion.current) return;
    persistence.adopt(metadata);
    if (result.snapshot_id && (metadata.snapshot?.snapshot_id !== result.snapshot_id || !metadata.snapshot.cache_available)) {
      discardDisplayedSnapshot();
      return;
    }
    acceptPage(result, result.spec ?? spec); setHistoryPaging(null); setSnapshotNotice('');
  }

  async function restoreHistory(value: RestoredSearch, repeat: boolean) {
    const previous = activeSearch.current;
    const version = ++searchVersion.current;
    showEditorForAction();
    activeSearch.current = null;
    setBusy(true); setLoadingMore(false); setPagingError(''); setError(''); closeContext();
    try {
      if (previous) {
        let job = await api<SearchJobStatus>(`/api/search-jobs/${encodeURIComponent(previous)}/cancel`, { method: 'POST' });
        while (!['completed', 'failed', 'cancelled'].includes(job.state) && version === searchVersion.current) {
          await new Promise(resolve => window.setTimeout(resolve, 350));
          job = await api<SearchJobStatus>(`/api/search-jobs/${encodeURIComponent(previous)}`);
        }
      }
      if (version !== searchVersion.current) return;
      applyIntent(value.entry.spec); setSearchJob(null); setSearchStopped(false);
      if (repeat) { void runSearch(value.entry.spec); return; }
      const bound = await persistence.bind(value.entry.snapshot_id, value.entry.spec,
        { visible_count: value.result?.results.length ?? 0, scroll_y: 0, display_settings_open: showSearchSettings });
      if (version !== searchVersion.current) return;
      const restored = await api<TabResult>(`/api/search-tabs/${tab.id}/result?snapshot_id=${encodeURIComponent(bound.snapshot?.snapshot_id ?? value.entry.snapshot_id)}`);
      if (version !== searchVersion.current) return;
      acceptRestored(restored, bound);
      if (activeRef.current) setCheckedActivation(activationRef.current);
      scroll.current = 0;
    } catch (error) { if (version === searchVersion.current) reportError(error); }
    finally { if (version === searchVersion.current) setBusy(false); }
  }

  async function toggleFavorite() {
    if (!savedSnapshotId || favoriteBusy) return;
    const version = searchVersion.current;
    setFavoriteBusy(true);
    try {
      const entry = await api<NonNullable<SearchTab['snapshot']>>(`/api/search-tabs/${tab.id}/favorite?snapshot_id=${encodeURIComponent(savedSnapshotId)}`, {
        method: 'POST', body: JSON.stringify({ favorite: !favorite }),
      });
      if (version === searchVersion.current) { setFavorite(entry.favorite); setSavedHistoryId(entry.id); }
    } catch (error) { if (version === searchVersion.current) reportError(error); }
    finally { setFavoriteBusy(false); }
  }

  async function runSearch(spec: SearchIntent) {
    const version = ++searchVersion.current;
    showEditorForAction();
    setBusy(true); setSearchJob(null); setSearchStopped(false); setError(''); setPagingError(''); setLoadingMore(false); closeContext();
    try {
      await persistence.flush();
      if (version !== searchVersion.current) return;
      let job = await api<SearchJobStatus>(`/api/search-tabs/${tab.id}/jobs`, { method: 'POST', body: JSON.stringify(spec) });
      if (version !== searchVersion.current) {
        void api(`/api/search-jobs/${encodeURIComponent(job.job_id)}/cancel`, { method: 'POST' }).catch(() => {});
        return;
      }
      await observe(job, spec, version);
    } catch (error) {
      if (version === searchVersion.current) {
        reportError(error);
        if (activeSearch.current) void api(`/api/search-jobs/${encodeURIComponent(activeSearch.current)}/cancel`, { method: 'POST' })
          .catch(error => { if (version === searchVersion.current) reportError(error); });
      }
    }
    finally { if (version === searchVersion.current) { setBusy(false); activeSearch.current = null; } }
  }

  async function stopSearch() {
    if (!activeSearch.current) return;
    const jobId = activeSearch.current;
    const version = searchVersion.current;
    setSearchJob(current => current?.job_id === jobId ? { ...current, state: 'cancelling' } : current);
    try {
      const job = await api<SearchJobStatus>(`/api/search-jobs/${encodeURIComponent(jobId)}/cancel`, { method: 'POST' });
      if (version === searchVersion.current && activeSearch.current === jobId) setSearchJob(job);
    } catch (error) {
      if (version === searchVersion.current && activeSearch.current === jobId) {
        setSearchJob(current => current?.job_id === jobId && current.state === 'cancelling'
          ? { ...current, state: 'running' } : current);
        reportError(error);
      }
    }
  }

  async function showMore() {
    if ((!searchId && !savedSnapshotId) || (nextOffset === null && !browseCursor) || loadingMore || busy) return;
    const version = searchVersion.current;
    const display = displayVersion.current;
    setLoadingMore(true); setPagingError('');
    try {
      const page = new URLSearchParams({ limit: String(submittedLimit) });
      if (browseCursor) page.set('cursor', browseCursor);
      else page.set('offset', String(nextOffset));
      let result: SearchPage;
      if (savedSnapshotId) {
        page.set('snapshot_id', savedSnapshotId);
        const restored = await api<TabResult>(`/api/search-tabs/${tab.id}/result?${page}`);
        if (!restored.result) {
          if (version === searchVersion.current && display === displayVersion.current) discardDisplayedSnapshot();
          throw new Error(t('Сохранённая выдача недоступна. Параметры и избранное сохранены; повторите поиск.'));
        }
        if (restored.entry?.snapshot_id !== savedSnapshotId) throw new Error(t('Сохранённая выдача изменилась. Откройте поиск из истории заново.'));
        result = restored.result;
      } else result = await api<SearchPage>(`/api/search-jobs/${encodeURIComponent(searchId!)}/result?${page}`);
      if (version !== searchVersion.current || display !== displayVersion.current) return;
      setHits(current => { const combined = [...(current ?? []), ...result.results]; return effectiveMode === 'browse' ? combined.slice(-100) : combined; });
      setNextOffset(result.next_offset ?? null); setBrowseCursor(result.browse_cursor ?? null); setHasMore(result.has_more);
    } catch (error) {
      if (version === searchVersion.current && display === displayVersion.current) setPagingError(error instanceof Error ? error.message : t('Ошибка соединения.'));
    } finally { if (version === searchVersion.current && display === displayVersion.current) setLoadingMore(false); }
  }

  async function openContext(hit: Hit, anchor = hit.message_id) {
    const version = ++contextVersion.current;
    setLoadingContext(true);
    const position = hit.messages.findIndex(message => message.message_id === hit.message_id);
    const before = Math.min(100, Math.max(15, position + 5));
    const after = Math.min(100, Math.max(15, hit.messages.length - position - 1 + 5));
    try {
      const result = await api<{ messages: Message[] }>(`/api/chats/${hit.chat_id}/context/${anchor}?before=${before}&after=${after}&${searchFilters}`);
      if (version === contextVersion.current) setContext({ hit, messages: result.messages });
    } catch (error) { if (version === contextVersion.current) reportError(error); }
    finally { if (version === contextVersion.current) setLoadingContext(false); }
  }

  const messageCount = chats.reduce((sum, chat) => sum + chat.messages, 0);
  const photoCount = chats.reduce((sum, chat) => sum + chat.photos, 0);
  const onlyImages = effectiveMode !== 'browse' && submittedModalities.length === 1 && submittedModalities[0] === 'images';
  const retrievalDisabled = !query.trim() ? 'Просмотр сообщений не использует ANN.' : exact
    ? 'Точная фраза не использует ANN.' : !modalities.length || (mode === 'words' && !modalities.includes('images'))
      ? 'Поиск только по словам не использует ANN.' : null;
  if (!active) return <div id={`search-pane-${tab.id}`} role="tabpanel" aria-labelledby={`search-tab-${tab.id}`} hidden />;
  if (transitioning || checkedActivation !== activation) return <div className="content" role="status">{t('Загрузка вкладки…')}</div>;
  return <>
      <div className="content" id={`search-pane-${tab.id}`} role="tabpanel" aria-labelledby={`search-tab-${tab.id}`} ref={content} onScroll={saveScroll}>
        <div className="heading"><h1>{t('Поиск')}</h1></div>
        {error && <div className="error" role="alert"><span>{t(error)}</span><button aria-label={t("Закрыть ошибку")} onClick={() => setError('')}><Icon name="close" /></button></div>}
        <form onSubmit={search} className="search-form">
          <div className="search-box"><span className="search-icon"><Icon name="search" size={24} /></span>
            <input aria-label={t("Поисковый запрос")} placeholder={t("Что вы хотите найти в переписке?")} value={query} onChange={event => setQuery(event.target.value)} />
            <button disabled={busy || !connected || !chatsLoaded || (!!query.trim() && !modalities.length)}>{busy ? t('Ищем…') : query.trim() ? t('Найти') : t('Показать сообщения')}<Icon name="arrow" size={18} /></button></div>
          <div className="filters">
            <label>{t("Режим")}<select aria-label={t("Режим поиска")} value={mode} disabled={exact} onChange={event => setMode(event.target.value)}><option value="words">{t("По словам")}</option><option value="meaning">{t("По смыслу")}</option><option value="hybrid">{t("Слова и смысл")}</option></select></label>
            <label>{t("Автор")}<select aria-label={t("Автор")} value={author} onChange={event => setAuthor(event.target.value)}><option value="">{t("Все авторы")}</option>{authorIds.length > 1 && <option value="__multiple__" disabled>{t('Выбрано авторов: {p0}', { p0: authorIds.length })}</option>}{authorIds.filter(id => !authors.some(item => item.author_id === id)).map(id => <option key={id} value={id}>{id}</option>)}{authors.map(item => <option value={item.author_id} key={item.author_id}>{item.name || item.author_id}</option>)}</select></label>
            <DateField label="С даты (UTC)" value={from} onChange={setFrom} />
            <DateField label="По дату (UTC)" value={to} onChange={setTo} />
            <label>{t("Содержимое")}<select value={contentType} onChange={event => setContentType(event.target.value)}><option value="all">{t("Все сообщения")}</option><option value="text">{t("С текстом")}</option><option value="photo">{t("С фотографией")}</option></select></label>
          </div>
          <RetrievalChoice value={strategy} onChange={setStrategy} disabledReason={retrievalDisabled} busy={busy} id={`retrieval-hint-${tab.id}`} />
          <ForwardFilter chatIds={selected} selected={forwardedFrom} onChange={setForwardedFrom} excludeDeleted={excludeDeleted} version={authorsVersion} disabled={busy} />
          <div className="search-options"><label><input type="checkbox" checked={exact} onChange={event => setExact(event.target.checked)} />{t("Точная фраза")}</label>
            <label><input type="checkbox" checked={excludeDeleted} onChange={event => setExcludeDeleted(event.target.checked)} />{t('Скрыть удалённые в Telegram')}</label>
            <div className="search-option-actions">
              <button type="button" className="text-button" aria-expanded={showSearchSettings} aria-controls="search-display-settings" onClick={() => setShowSearchSettings(value => !value)}><Icon name="chevron" size={14} style={{ transform: showSearchSettings ? 'rotate(90deg)' : undefined }} /> <span>{t('Выдача')}</span></button>
              <button type="button" className="text-button" onClick={() => { setAuthor(''); setForwardedFrom([]); setFrom(''); setTo(''); setContentType('all'); setExact(false); setExcludeDeleted(false); setScope(null); }}>{t("Сбросить фильтры")}</button>
            </div></div>
          <fieldset className="search-modalities" disabled={busy}>
            <legend>{t("Искать в")}</legend>
            <div className="modality-options">
              <button type="button" aria-pressed={modalities.length === allModalities.length} onClick={() => changeModalities([...allModalities])}>{t("Всё")}</button>
              {allModalities.map(kind => <label key={kind} className={modalities.includes(kind) ? 'selected' : ''}>
                <input type="checkbox" checked={modalities.includes(kind)} onChange={() => changeModalities(allModalities.filter(value => value === kind ? !modalities.includes(kind) : modalities.includes(value)))} />
                {t(modalityLabels[kind])}
              </label>)}
            </div>
            {!!query.trim() && !modalities.length && <p className="warning" role="status">{t("Выберите хотя бы один тип поиска.")}</p>}
          </fieldset>
        </form>
        <section id="search-display-settings" className="search-display-options" aria-label={t('Выдача')} hidden={!showSearchSettings}><SearchSettingsPanel key={preferencesVersion} initialSettings={searchPreferences} onSaved={setSearchPreferences} /></section>
        <hr className="search-divider" />
        {busy && <SearchProgress status={searchJob} onStop={() => void stopSearch()} />}
        {searchStopped && <p role="status">{t('Поиск остановлен')}</p>}
        {snapshotNotice && <p className="snapshot-notice" role="status">{t(snapshotNotice)}{' '}
          <button type="button" className="text-button" disabled={busy || !submittedIntent} onClick={() => {
            if (submittedIntent) { applyIntent(submittedIntent); void runSearch(submittedIntent); }
          }}>{t('Повторить поиск')}</button></p>}
        {exact && <p className="baseline-note">{t("Точная фраза ищется в сообщениях и распознанном тексте фотографий.")}</p>}
        <details className="archive-status"><summary>{t('Состояние индексов')}</summary>
          <p>{t('Индекс: {p0} · Поиск: {p1}', { p0: indexingDevices, p1: searchDevices })}</p>
        {semantic?.enabled === 1 && <p className="baseline-note">{t("Смысловой индекс: ")}{semantic.ready_segments} / {semantic.total_segments}{t(" сегментов")}{semantic.paused ? t(' · на паузе') : ''}</p>}
        {media && (media.ocr_enabled === 1 || media.images_enabled === 1) && <p className="baseline-note">{t("Фотографии: ")}{media.images_ready} / {media.total_photos} · OCR: {media.ocr_ready} / {media.total_photos}{t(" · OCR по смыслу: ")}{media.ocr_dense_ready}{media.paused ? t(' · медиа на паузе') : ''}</p>}
        </details>
        {reranked && hits !== null && !busy && <p className="baseline-note" role="status">{t('Порядок результатов уточнён.')}</p>}
        {warnings.map(warning => <p className="warning" role="status" key={warning}>{t(warning)}</p>)}

        {hits === null ? <section className="welcome">
          <h2>{chatsLoaded ? chats.length ? t('Поиск по архиву') : t('Добавьте сообщения') : t('Загрузка диалогов…')}</h2>
          <p>{chats.length ? t('Введите запрос или покажите сообщения по выбранным фильтрам.') : t('Импортируйте JSON одного диалога из Telegram Desktop или подключите Telegram.')}</p>
          <div className="stats"><div><strong>{messageCount.toLocaleString(uiLocale())}</strong><span>{t("сообщений")}</span></div><div><strong>{chats.length}</strong><span>{t("диалогов")}</span></div><div><strong>{photoCount}</strong><span>{t("фотографий")}</span></div></div>
        </section> : <section className="results" aria-live="polite">
          <div className="results-heading"><h2>{hits.length ? t(effectiveMode === 'browse' ? 'Найдено сообщений: {p0}{p1}' : 'Найдено фрагментов: {p0}{p1}', { p0: hits.length, p1: hasMore ? '+' : '' }) : t('Совпадений пока нет')}</h2><span>{effectiveMode === 'browse' ? t('Просмотр сообщений') : onlyImages ? t('По описанию · изображения') : effectiveMode === 'mixed' ? t("{p0} · общая выдача", { p0: submittedModalities.map(kind => t(modalityLabels[kind])).join(' + ') }) : effectiveMode === 'hybrid' ? t('Слова и смысл') : effectiveMode === 'meaning' ? t('По смыслу') : t('По словам')}</span></div>
          <div className="submitted-search-actions"><p className="submitted-search">{submitted ? t('Результаты запроса: {p0}', { p0: submitted }) : t('Сообщения по выполненным фильтрам')}</p>
            {savedSnapshotId && <FavoriteButton selected={favorite} disabled={favoriteBusy || busy} onClick={() => void toggleFavorite()} />}</div>
          <RetrievalSummary info={retrieval} />
          {!hits.length && <div className="no-results">{effectiveMode === 'browse' ? t('По выбранным фильтрам сообщений нет. Измените фильтры и нажмите «Показать сообщения».') : <>{t("Попробуйте другой запрос или расширьте область поиска.")}{onlyImages ? t(' Проверьте готовность индекса фотографий.') : effectiveMode === 'words' ? t(' Поиск по словам требует все слова запроса.') : t(' Проверьте готовность выбранных индексов.')}</>}</div>}
          <div className={onlyImages ? 'photo-grid' : 'result-list'}>{hits.map((hit, index) => <article className="result-card" key={hit.chunk_id || `${hit.chat_id}/${hit.message_id}`}>
            <div className="result-header"><span><Icon name="archive" size={18} />{hit.chat_name}</span><small>{t('Результат #{p0}', { p0: index + 1 })}</small></div>
            <details className="result-details"><summary>{t('Подробности совпадения')}</summary><div className="result-ranking"><span>{t('Сообщение #{p0}', { p0: hit.message_id })}</span>
              {hit.image_similarity != null && Number.isFinite(hit.image_similarity) && <span title={t('Сходство изображения с описанием: от −1 до 1. Чем выше, тем ближе совпадение; это не вероятность.')}>
                {t('Сходство изображения: {p0}', { p0: hit.image_similarity.toLocaleString(uiLocale(), { minimumFractionDigits: 4, maximumFractionDigits: 4 }) })}
              </span>}
            </div></details>
            {hit.matched_by && <div className="match-reasons">{hit.matched_by.map(reason => t(reasons[reason])).join(' · ')}</div>}
            {hit.messages.map(message => <MessageRow key={message.message_id} message={message} anchor={hit.message_id} query={submitted} onOpenImage={setOpenImage} />)}
            {hit.matched_parts?.some(part => !hit.messages.some(message => message.message_id === part.message_id)) && <p className="baseline-note">{t("Показана часть найденного фрагмента. Другие сообщения доступны через «Открыть контекст».")}</p>}
            {hit.ocr_match && hit.ocr_match.kind !== 'exact' && <p className="baseline-note ocr-match">{hit.ocr_match.kind === 'substring' ? t('OCR: совпала часть слова') : t('OCR: неточное совпадение · отличий: {p0}', { p0: hit.ocr_match.edits })}</p>}
            {hit.ocr_text && <details className="ocr-evidence"><summary>{t("Распознанный текст")}{hit.ocr_confidence != null ? t(" · уверенность OCR {p0} / 100", { p0: Math.round(hit.ocr_confidence) }) : ''}</summary><div className="message-text"><Highlight text={hit.ocr_text} query={submitted} /></div><p>{t("Распознавание может содержать ошибки. Откройте фотографию для проверки.")}</p></details>}
            <button className="context-button" disabled={loadingContext} onClick={() => void openContext(hit)}>{t("Открыть контекст ")}<Icon name="arrow" size={16} /></button>
          </article>)}</div>
          {(nextOffset !== null || browseCursor) && <div className="more-results"><button type="button" className="primary" disabled={busy || loadingMore} onClick={() => void showMore()}>{loadingMore ? t('Загружаем…') : t('Показать ещё')}</button></div>}
          {pagingError && <p className="error" role="alert">{t(pagingError)}</p>}
          {hasMore && nextOffset === null && !browseCursor && <p className="more-note">{t('Показаны первые {p0} фрагментов. Уточните запрос или фильтры, чтобы найти больше.', { p0: hits.length })}</p>}
        </section>}
      </div>
    {context && <div className="overlay"><section className="modal context-modal" role="dialog" aria-modal="true" aria-labelledby="context-title">
      <button className="close" aria-label={t("Закрыть контекст")} onClick={closeContext}><Icon name="close" /></button><div className="eyebrow">{t("КОНТЕКСТ ДИАЛОГА")}</div><h2 id="context-title">{context.hit.chat_name}</h2>
      <div className="context-nav"><button disabled={loadingContext} onClick={() => void openContext(context.hit, context.messages[0].message_id)}><Icon name="left" size={16} />{t('Более ранние')}</button><button disabled={loadingContext} onClick={() => void openContext(context.hit, context.messages[context.messages.length - 1].message_id)}>{t('Более поздние')}<Icon name="arrow" size={16} /></button></div>
      <div className="context-messages">{context.messages.map(message => <MessageRow key={message.message_id} message={message} anchor={context.hit.message_id} query={submitted} onOpenImage={setOpenImage} />)}</div>
    </section></div>}
    {openImage && <ImageViewer key={`${openImage.id}/${openImage.messageId}`} image={openImage} onClose={() => setOpenImage(null)} />}
  </>;
});
export default SearchPane;
