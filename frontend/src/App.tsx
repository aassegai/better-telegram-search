import { getLanguage, t, uiLocale, useLanguage } from './i18n';
import { useEffect, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import { api, initializeSession } from './api';
import ImportDialog from './ImportDialog';
import ImportProgress from './ImportProgress';
import ConflictDialog from './ConflictDialog';
import ChatIndexDialog from './ChatIndexDialog';
import TelegramPanel from './TelegramPanel';
import ForwardFilter from './ForwardFilter';
import type { Update } from './UpdatePanel';
import SearchSettingsPanel from './SearchSettingsPanel';
import SearchProgress from './SearchProgress';
import SettingsCenter, { initialSettingsSection } from './SettingsCenter';
import type { SettingsSection } from './SettingsCenter';
import Icon from './Icon';
import LanguageSwitch from './LanguageSwitch';
import DateField from './DateField';
import SettingsDialog from './SettingsDialog';
import ImageViewer from './ImageViewer';
import type { OpenImage } from './ImageViewer';
import { useTheme } from './theme';
import './theme.css';
import type { Chat, Hit, Job, MediaStatus, Message, Preview, SearchJobStatus, SearchModality, SemanticStatus } from './types';

const dates = {
  ru: new Intl.DateTimeFormat('ru-RU', { dateStyle: 'medium', timeStyle: 'short' }),
  en: new Intl.DateTimeFormat('en-US', { dateStyle: 'medium', timeStyle: 'short' }),
};
const date = (value: number) => dates[getLanguage()].format(new Date(value * 1000));
const normalize = (text: string) => text.normalize('NFKC').toLocaleLowerCase('ru').replaceAll('ё', 'е');
const stateNames: Record<string, string> = {
  queued: 'В очереди', running: 'Импортируется', completed: 'Поиск доступен',
  paused: 'Приостановлено', interrupted: 'Прервано', cancelled: 'Отменено', failed: 'Ошибка',
};
const reasons: Record<string, string> = { words: 'Совпали слова', meaning: 'Близкий смысл', image: 'Фотография по описанию', ocr_words: 'Слова на фотографии', ocr_meaning: 'Смысл текста на фотографии' };
type SearchPage = { results: Hit[]; has_more: boolean; effective_mode: string; warnings: string[];
  rerank_applied?: boolean; limit: number; job_id: string; browse_cursor?: string | null;
  next_offset?: number | null; cached_results?: number };

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

export default function App() {
  const [language, changeLanguage] = useLanguage();
  const [theme, toggleTheme] = useTheme();
  const [chats, setChats] = useState<Chat[]>([]);
  const [chatsLoaded, setChatsLoaded] = useState(false);
  const [selected, setSelected] = useState<string[] | null>(null);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [previews, setPreviews] = useState<Preview[]>([]);
  const [activePreview, setActivePreview] = useState<Preview | null>(null);
  const [activeImport, setActiveImport] = useState<Job | null>(null);
  const [authors, setAuthors] = useState<{ author_id: string; name: string }[]>([]);
  const [query, setQuery] = useState('');
  const [submitted, setSubmitted] = useState('');
  const [author, setAuthor] = useState('');
  const [forwardedFrom, setForwardedFrom] = useState<string[]>([]);
  const [from, setFrom] = useState('');
  const [to, setTo] = useState('');
  const [contentType, setContentType] = useState('all');
  const [exact, setExact] = useState(false);
  const [excludeDeleted, setExcludeDeleted] = useState(false);
  const [mode, setMode] = useState('hybrid');
  const [effectiveMode, setEffectiveMode] = useState('words');
  const [warnings, setWarnings] = useState<string[]>([]);
  const [reranked, setReranked] = useState(false);
  const [semantic, setSemantic] = useState<SemanticStatus | null>(null);
  const [media, setMedia] = useState<MediaStatus | null>(null);
  const [modalities, setModalities] = useState<SearchModality[]>(allModalities);
  const [submittedModalities, setSubmittedModalities] = useState<SearchModality[]>(allModalities);
  const [deleting, setDeleting] = useState(false);
  const [hits, setHits] = useState<Hit[] | null>(null);
  const [hasMore, setHasMore] = useState(false);
  const [submittedLimit, setSubmittedLimit] = useState(20);
  const [searchId, setSearchId] = useState<string | null>(null);
  const [nextOffset, setNextOffset] = useState<number | null>(null);
  const [browseCursor, setBrowseCursor] = useState<string | null>(null);
  const [searchJob, setSearchJob] = useState<SearchJobStatus | null>(null);
  const [searchStopped, setSearchStopped] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [pagingError, setPagingError] = useState('');
  const searchVersion = useRef(0);
  const activeSearch = useRef<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [showSearchSettings, setShowSearchSettings] = useState(false);
  const [connected, setConnected] = useState(false);
  const [error, setError] = useState('');
  const [modal, setModal] = useState<'import' | 'settings' | 'sources' | null>(null);
  const [indexChat, setIndexChat] = useState<Chat | null>(null);
  const [conflictJob, setConflictJob] = useState<Job | null>(null);
  const [context, setContext] = useState<{ hit: Hit; messages: Message[] } | null>(null);
  const [openImage, setOpenImage] = useState<OpenImage | null>(null);
  const [loadingContext, setLoadingContext] = useState(false);
  const [settingsSection, setSettingsSection] = useState<SettingsSection>(initialSettingsSection);
  const [performanceScope, setPerformanceScope] = useState<string | null>(null);
  const authorsVersion = jobs.map(job => `${job.id}:${job.processed}:${job.state}:${job.pending_conflicts}`).join('|');
  const contextVersion = useRef(0);
  const [searchFilters, setSearchFilters] = useState('');
  const closeContext = () => { contextVersion.current++; setContext(null); setLoadingContext(false); };
  const devices = (values: (string | undefined)[]) => [...new Set(values.filter(Boolean).flatMap(value => value!.toUpperCase().split('+').map(device => device.trim())))].join(' + ') || 'CPU';
  const indexingDevices = devices([semantic?.backend?.device, media?.backend?.device,
    media?.ocr_enabled ? media.ocr_backend?.device : undefined]);
  const searchDevices = devices([semantic?.backend?.query_execution?.device, media?.query_backend?.device]);

  const restarting = useRef(false);
  const updateObserverRevision = useRef(0);
  const refreshRequests = useRef(new Map<string, Promise<void>>());
  const reportError = (error: unknown) => { if (!restarting.current) setError(error instanceof Error ? error.message : t('Ошибка соединения.')); };
  const refreshEndpoint = <T,>(path: string, accept: (value: T) => void): Promise<void> => {
    const pending = refreshRequests.current.get(path);
    if (pending) return pending;
    const request = api<T>(path).then(accept).catch(reportError).finally(() => refreshRequests.current.delete(path));
    refreshRequests.current.set(path, request);
    return request;
  };
  const refresh = async () => {
    void refreshEndpoint<SemanticStatus>('/api/semantic', setSemantic);
    void refreshEndpoint<MediaStatus>('/api/media-index', setMedia);
    await Promise.allSettled([
      refreshEndpoint<Chat[]>('/api/chats', value => {
        setChats(value); setChatsLoaded(true);
        setPerformanceScope(previous => previous && !value.some(chat => chat.id === previous) ? null : previous);
      }),
      refreshEndpoint<Job[]>('/api/imports', setJobs), refreshEndpoint<Preview[]>('/api/import-previews', setPreviews),
    ]);
  };

  useEffect(() => {
    let alive = true;
    let pending = false;
    const poll = async () => {
      if (!alive || pending) return;
      pending = true;
      try { await refresh(); }
      catch (error) { if (alive) reportError(error); }
      finally { pending = false; }
    };
    initializeSession().then(() => {
      if (alive) { setConnected(true); void poll(); }
    }).catch(reportError);
    const timer = window.setInterval(() => void poll(), 2000);
    return () => { alive = false; clearInterval(timer); };
  }, []);

  useEffect(() => {
    let alive = true;
    let pending = false;
    const poll = async () => {
      if (pending) return;
      pending = true;
      try {
        const revision = updateObserverRevision.current;
        const update = await api<Update>('/api/updates');
        if (!alive || revision !== updateObserverRevision.current) return;
        if (update.state === 'installing') restarting.current = true;
        if (restarting.current && ['updated', 'rolled_back'].includes(update.state)) window.location.reload();
        if (restarting.current && update.state === 'failed') {
          restarting.current = false;
          setError(update.error || t('Не удалось обновить приложение.'));
        }
      } catch { /* The server is temporarily absent while restarting. */ }
      finally { pending = false; }
    };
    void poll();
    const timer = window.setInterval(() => void poll(), 1000);
    return () => { alive = false; window.clearInterval(timer); };
  }, []);

  useEffect(() => {
    const params = new URLSearchParams();
    selected?.forEach(id => params.append('chat_id', id));
    let alive = true;
    api<{ author_id: string; name: string }[]>(`/api/authors?${params}`).then(authors => {
      if (alive) {
        setAuthors(authors);
        setAuthor(current => authors.some(item => item.author_id === current) ? current : '');
      }
    }).catch(reportError);
    return () => { alive = false; };
  }, [selected, chats.length, authorsVersion]);

  useEffect(() => {
    const escape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') { setModal(null); setIndexChat(null); setConflictJob(null); closeContext(); }
    };
    window.addEventListener('keydown', escape);
    return () => window.removeEventListener('keydown', escape);
  }, []);

  const toggleChat = (id: string) => setSelected(current => {
    const chosen = current ?? chats.map(chat => chat.id);
    return chosen.includes(id) ? chosen.filter(value => value !== id) : [...chosen, id];
  });

  const changeModalities = (next: SearchModality[]) => {
    setModalities(next);
  };

  async function search(event: FormEvent) {
    event.preventDefault();
    if (busy) return;
    if (query.trim() && !modalities.length) { setError(t('Выберите хотя бы один тип поиска.')); return; }
    const version = ++searchVersion.current;
    setBusy(true); setSearchJob(null); setSearchStopped(false); setError(''); setPagingError(''); setLoadingMore(false); closeContext();
    const params = new URLSearchParams({ q: query, exact: String(exact), content_type: contentType, mode, exclude_deleted: String(excludeDeleted) });
    modalities.forEach(kind => params.append('modality', kind));
    selected?.forEach(id => params.append('chat_id', id));
    if (author) params.set('author_id', author);
    forwardedFrom.forEach(name => params.append('forwarded_from', name));
    if (from) params.set('date_from', from);
    if (to) params.set('date_to', to);
    try {
      let job = await api<SearchJobStatus>('/api/search-jobs', { method: 'POST', body: JSON.stringify({
        query, scope: selected === null ? { mode: 'all' } : { mode: 'selected', chat_ids: selected },
        author_ids: author ? [author] : [], forwarded_from: forwardedFrom,
        date_from: from || null, date_to: to || null, content_type: contentType,
        exclude_deleted: excludeDeleted, exact_phrase: exact, mode,
        modalities: modalities.length ? modalities : allModalities,
      }) });
      if (version !== searchVersion.current) {
        void api(`/api/search-jobs/${encodeURIComponent(job.job_id)}/cancel`, { method: 'POST' }).catch(reportError);
        return;
      }
      activeSearch.current = job.job_id;
      while (version === searchVersion.current) {
        const snapshot = job;
        setSearchJob(current => current?.job_id === snapshot.job_id && current?.state === 'cancelling' && ['queued', 'running'].includes(snapshot.state) ? current : snapshot);
        if (['completed', 'cancelled', 'failed'].includes(job.state)) break;
        await new Promise(resolve => window.setTimeout(resolve, 350));
        job = await api<SearchJobStatus>(`/api/search-jobs/${encodeURIComponent(job.job_id)}`);
      }
      if (version !== searchVersion.current) return;
      if (job.state === 'cancelled') { setSearchStopped(true); return; }
      if (job.state === 'failed') throw new Error(job.error || t('Не удалось выполнить поиск.'));
      const result = await api<SearchPage>(`/api/search-jobs/${encodeURIComponent(job.job_id)}/result`);
      if (version !== searchVersion.current) return;
      setSearchId(job.job_id); setNextOffset(result.next_offset ?? null); setBrowseCursor(result.browse_cursor ?? null);
      setHits(result.results); setHasMore(result.has_more); setSubmitted(query);
      setSubmittedLimit(result.limit ?? 20);
      setEffectiveMode(result.effective_mode); setWarnings(result.warnings ?? []); setReranked(Boolean(result.rerank_applied));
      setSearchFilters(params.toString());
      setSubmittedModalities([...modalities]);
    } catch (error) {
      if (version === searchVersion.current) {
        reportError(error);
        if (activeSearch.current) void api(`/api/search-jobs/${encodeURIComponent(activeSearch.current)}/cancel`, { method: 'POST' }).catch(reportError);
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
    if (!searchId || (nextOffset === null && !browseCursor) || loadingMore || busy) return;
    const version = searchVersion.current;
    setLoadingMore(true); setPagingError('');
    try {
      const page = new URLSearchParams({ limit: String(submittedLimit) });
      if (browseCursor) page.set('cursor', browseCursor);
      else page.set('offset', String(nextOffset));
      const result = await api<SearchPage>(`/api/search-jobs/${encodeURIComponent(searchId)}/result?${page}`);
      if (version !== searchVersion.current) return;
      setHits(current => [...(current ?? []), ...result.results]);
      setNextOffset(result.next_offset ?? null); setBrowseCursor(result.browse_cursor ?? null); setHasMore(result.has_more);
    } catch (error) {
      if (version === searchVersion.current) setPagingError(error instanceof Error ? error.message : t('Ошибка соединения.'));
    } finally { if (version === searchVersion.current) setLoadingMore(false); }
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

  async function control(job: Job, action: string) {
    try { await api(`/api/imports/${job.id}/${action}`, { method: 'POST' }); await refresh(); }
    catch (error) { reportError(error); }
  }

  async function deleteChat(chat: Chat) {
    if (deleting) return;
    setDeleting(true);
    try {
      const estimate = await api<{ messages: number; message_text_bytes: number; exclusive_media: number; shared_media: number }>(`/api/chats/${chat.id}/deletion-estimate`);
      if (!window.confirm(t("Удалить «{p0}» и его индекс из приложения? {p1} сообщений, {p2} КиБ текста. Кэш {p3} вложений удалится; общих вложений {p4}. Точное освобождение места зависит от уплотнения индекса. Исходный экспорт сохранится.", { p0: chat.name, p1: estimate.messages, p2: (estimate.message_text_bytes / 1024).toFixed(1), p3: estimate.exclusive_media, p4: estimate.shared_media }))) return;
      await api(`/api/chats/${chat.id}`, { method: 'DELETE' });
      if (activeSearch.current) void api(`/api/search-jobs/${encodeURIComponent(activeSearch.current)}/cancel`, { method: 'POST' }).catch(reportError);
      activeSearch.current = null; setBusy(false); setBrowseCursor(null);
      searchVersion.current++; setSearchId(null); setNextOffset(null); setLoadingMore(false); setPagingError('');
      setSelected(null); setHits(null); setReranked(false); closeContext(); await refresh();
    } catch (error) { reportError(error); }
    finally { setDeleting(false); }
  }

  function settings(section?: SettingsSection, scope?: string | null) {
    if (section) { setSettingsSection(section); try { localStorage.setItem('bts.settings-section', section); } catch { /* The current selection remains in memory. */ } }
    if (scope !== undefined) setPerformanceScope(scope);
    setModal('settings');
  }

  const messageCount = chats.reduce((sum, chat) => sum + chat.messages, 0);
  const photoCount = chats.reduce((sum, chat) => sum + chat.photos, 0);
  const onlyImages = effectiveMode !== 'browse' && submittedModalities.length === 1 && submittedModalities[0] === 'images';
  return <div className="app">
    <aside className="sidebar">
      <a href="/" className="brand"><span className="brand-icon"><Icon name="archive" size={26} /></span>
        <span>{t("Архив")}<small>TELEGRAM SEARCH</small></span></a>
      <div className="sidebar-content">
      <div className="sidebar-title"><span>{t("Диалоги")}</span><button className="text-button" onClick={() => setSelected(null)}>{t("Все")}</button></div>
      <div className="chat-list">
        {chats.map(chat => <div className="chat-item" key={chat.id}>
          <label><input type="checkbox" checked={selected === null || selected.includes(chat.id)} onChange={() => toggleChat(chat.id)} />
            <span><strong>{chat.name}</strong><small>{chat.messages.toLocaleString(uiLocale())}{t(" сообщений · ")}{chat.photos}{t(" фото")}</small></span></label>
          <button className="chat-settings-button" aria-label={t('Настройки индексации {p0}', { p0: chat.name })} title={t('Настройки индексации')} onClick={() => setIndexChat(chat)}><Icon name="more" /></button>
          <button className="delete-button" disabled={deleting} aria-label={t("Удалить {p0}", { p0: chat.name })} onClick={() => void deleteChat(chat)}><Icon name="close" /></button>
        </div>)}
        {!chatsLoaded && <p className="sidebar-empty" role="status">{t('Загрузка диалогов…')}</p>}
        {chatsLoaded && !chats.length && <p className="sidebar-empty">{t("Добавьте экспорт, чтобы ваша переписка стала доступна для поиска.")}</p>}
      </div>
      <button className="import-button telegram-sources-button" disabled={!connected} onClick={() => setModal('sources')}>{t('Источники · Telegram')}</button>
      <button className="import-button" disabled={!connected} onClick={() => { setActivePreview(null); setActiveImport(null); setModal('import'); }}><Icon name="plus" />{t("Импорт сообщений")}</button>
      {previews.length > 0 && <div className="jobs"><div className="sidebar-title">{t("Проверки экспорта")}</div>
        {previews.map(preview => <div className="job" key={preview.id}><small><strong>{preview.chat_name || t('Подготовка выгрузки')}</strong> · {preview.scope}</small>
          <ImportProgress progress={preview.progress} state={preview.state} compact />
          <div className="job-actions"><button onClick={() => { setActivePreview(preview); setActiveImport(null); setModal('import'); }}>{t("Открыть отчёт")}</button></div></div>)}
      </div>}
      {jobs.length > 0 && <div className="jobs"><div className="sidebar-title">{t("Последние импорты")}</div>
        {jobs.slice(0, 4).map(job => <div className="job" key={job.id}>
          <small><strong>{job.chat_name}</strong></small>
          <div className={`job-state ${job.state}`}><span className="dot" />{t(stateNames[job.state] || job.state)}</div>
          <ImportProgress progress={job.progress} state={job.state} compact />
          <small>{job.processed}{t(" обработано · ")}{job.added}{t(" новых · ")}{job.updated}{t(" обновлено")}</small>
          {job.pending_conflicts > 0 && <small className="warning">{t("Неразрешённых конфликтов: ")}{job.pending_conflicts}</small>}
          {(job.missing_media + job.invalid_media) > 0 && <small className="warning">{t("Недоступных вложений: ")}{job.missing_media + job.invalid_media}</small>}
          {job.error && <small className="warning">{t(job.error)}</small>}
          {job.warnings?.map(warning => <small className="warning" key={warning}>{t(warning)}</small>)}
          <div className="job-actions">
            <button onClick={() => { setActiveImport(job); setActivePreview(null); setModal('import'); }}>{t('Открыть импорт')}</button>
            {job.pending_conflicts > 0 && <button onClick={() => setConflictJob(job)}>{t("Разобрать конфликты")}</button>}
            {['queued', 'running'].includes(job.state) && <button onClick={() => void control(job, 'pause')}>{t("Пауза")}</button>}
            {['paused', 'interrupted', 'failed'].includes(job.state) && <button onClick={() => void control(job, 'resume')}>{t("Продолжить")}</button>}
            {['running', 'queued', 'paused', 'interrupted'].includes(job.state) && <button onClick={() => void control(job, 'cancel')}>{t("Отменить")}</button>}
          </div>
        </div>)}
      </div>}
      </div>
      <div className="sidebar-footer"><button className="settings-button" onClick={() => void settings()}><Icon name="settings" />{t('Настройки')}</button><span><i className="dot" />{t("Локально на этом компьютере")}</span></div>
    </aside>

    <main className="main">
      <header className="topbar"><div className="topbar-actions">
        <button className="theme-toggle mobile-settings-button" type="button" aria-label={t('Настройки')} title={t('Настройки')} onClick={() => void settings()}><Icon name="settings" /></button>
        <button className="theme-toggle" type="button" aria-label={t('Тёмная тема')} aria-pressed={theme === 'dark'} title={t('Тёмная тема')} onClick={toggleTheme}><Icon name={theme === 'dark' ? 'sun' : 'moon'} /></button>
        {!modal && !indexChat && <LanguageSwitch value={language} onChange={changeLanguage} />}
      </div></header>
      <div className="content">
        <div className="heading"><h1>{t('Поиск')}</h1></div>
        {error && <div className="error" role="alert"><span>{t(error)}</span><button aria-label={t("Закрыть ошибку")} onClick={() => setError('')}><Icon name="close" /></button></div>}
        <form onSubmit={search} className="search-form">
          <div className="search-box"><span className="search-icon"><Icon name="search" size={24} /></span>
            <input aria-label={t("Поисковый запрос")} placeholder={t("Что вы хотите найти в переписке?")} value={query} onChange={event => setQuery(event.target.value)} />
            <button disabled={busy || !connected || !chatsLoaded || (!!query.trim() && !modalities.length)}>{busy ? t('Ищем…') : query.trim() ? t('Найти') : t('Показать сообщения')}<Icon name="arrow" size={18} /></button></div>
          <div className="filters">
            <label>{t("Режим")}<select aria-label={t("Режим поиска")} value={mode} disabled={exact} onChange={event => setMode(event.target.value)}><option value="words">{t("По словам")}</option><option value="meaning">{t("По смыслу")}</option><option value="hybrid">{t("Слова и смысл")}</option></select></label>
            <label>{t("Автор")}<select aria-label={t("Автор")} value={author} onChange={event => setAuthor(event.target.value)}><option value="">{t("Все авторы")}</option>{authors.map(item => <option value={item.author_id} key={item.author_id}>{item.name || item.author_id}</option>)}</select></label>
            <DateField label="С даты (UTC)" value={from} onChange={setFrom} />
            <DateField label="По дату (UTC)" value={to} onChange={setTo} />
            <label>{t("Содержимое")}<select value={contentType} onChange={event => setContentType(event.target.value)}><option value="all">{t("Все сообщения")}</option><option value="text">{t("С текстом")}</option><option value="photo">{t("С фотографией")}</option></select></label>
          </div>
          <ForwardFilter chatIds={selected} selected={forwardedFrom} onChange={setForwardedFrom} excludeDeleted={excludeDeleted} version={authorsVersion} disabled={busy} />
          <div className="search-options"><label><input type="checkbox" checked={exact} onChange={event => setExact(event.target.checked)} />{t("Точная фраза")}</label>
            <label><input type="checkbox" checked={excludeDeleted} onChange={event => setExcludeDeleted(event.target.checked)} />{t('Скрыть удалённые в Telegram')}</label>
            <div className="search-option-actions">
              <button type="button" className="text-button" aria-expanded={showSearchSettings} aria-controls="search-display-settings" onClick={() => setShowSearchSettings(value => !value)}><Icon name="chevron" size={14} style={{ transform: showSearchSettings ? 'rotate(90deg)' : undefined }} /> <span>{t('Выдача')}</span></button>
              <button type="button" className="text-button" onClick={() => { setAuthor(''); setForwardedFrom([]); setFrom(''); setTo(''); setContentType('all'); setExact(false); setExcludeDeleted(false); setSelected(null); }}>{t("Сбросить фильтры")}</button>
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
        <section id="search-display-settings" className="search-display-options" aria-label={t('Выдача')} hidden={!showSearchSettings}><SearchSettingsPanel /></section>
        <hr className="search-divider" />
        {busy && <SearchProgress status={searchJob} onStop={() => void stopSearch()} />}
        {searchStopped && <p role="status">{t('Поиск остановлен')}</p>}
        {exact && <p className="baseline-note">{t("Точная фраза ищется в сообщениях и распознанном тексте фотографий.")}</p>}
        <details className="archive-status"><summary>{t('Состояние индексов')}</summary>
          <p>{t('Индекс: {p0} · Поиск: {p1}', { p0: indexingDevices, p1: searchDevices })}</p>
        {semantic?.enabled === 1 && <p className="baseline-note">{t("Смысловой индекс: ")}{semantic.ready_segments} / {semantic.total_segments}{t(" сегментов")}{semantic.paused ? t(' · на паузе') : ''}</p>}
        {media && (media.ocr_enabled === 1 || media.images_enabled === 1) && <p className="baseline-note">{t("Фотографии: ")}{media.images_ready} / {media.total_photos} · OCR: {media.ocr_ready} / {media.total_photos}{t(" · OCR по смыслу: ")}{media.ocr_dense_ready}{media.paused ? t(' · медиа на паузе') : ''}</p>}
        </details>
        {reranked && hits !== null && !busy && <p className="baseline-note" role="status">{t('Порядок текста и OCR уточнён Giga')}</p>}
        {warnings.map(warning => <p className="warning" role="status" key={warning}>{t(warning)}</p>)}

        {hits === null ? <section className="welcome">
          <h2>{chatsLoaded ? chats.length ? t('Поиск по архиву') : t('Добавьте сообщения') : t('Загрузка диалогов…')}</h2>
          <p>{chats.length ? t('Введите запрос или покажите сообщения по выбранным фильтрам.') : t('Импортируйте JSON одного диалога из Telegram Desktop или подключите Telegram.')}</p>
          <div className="stats"><div><strong>{messageCount.toLocaleString(uiLocale())}</strong><span>{t("сообщений")}</span></div><div><strong>{chats.length}</strong><span>{t("диалогов")}</span></div><div><strong>{photoCount}</strong><span>{t("фотографий")}</span></div></div>
        </section> : <section className="results" aria-live="polite">
          <div className="results-heading"><h2>{hits.length ? t(effectiveMode === 'browse' ? 'Найдено сообщений: {p0}{p1}' : 'Найдено фрагментов: {p0}{p1}', { p0: hits.length, p1: hasMore ? '+' : '' }) : t('Совпадений пока нет')}</h2><span>{effectiveMode === 'browse' ? t('Просмотр сообщений') : onlyImages ? t('По описанию · изображения') : effectiveMode === 'mixed' ? t("{p0} · общая выдача", { p0: submittedModalities.map(kind => t(modalityLabels[kind])).join(' + ') }) : effectiveMode === 'hybrid' ? t('Слова и смысл') : effectiveMode === 'meaning' ? t('По смыслу') : t('По словам')}</span></div>
          <p className="submitted-search">{submitted ? t('Результаты запроса: {p0}', { p0: submitted }) : t('Сообщения по выполненным фильтрам')}</p>
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
      </div><footer className="main-footer">{t("Сообщения хранятся и обрабатываются на этом компьютере.")}</footer>
    </main>

    {modal === 'import' && <ImportDialog chats={chats} initialPreview={activePreview} initialImport={activeImport} onClose={() => setModal(null)} onApplied={() => { void refresh().catch(reportError); }} />}
    {conflictJob && <div className="overlay"><ConflictDialog job={conflictJob} onClose={() => setConflictJob(null)} onChanged={refresh} /></div>}
    {indexChat && <ChatIndexDialog onResources={() => { setIndexChat(null); settings('performance', null); }} onSources={() => { setIndexChat(null); setModal('sources'); }} onModels={() => { setIndexChat(null); settings('models', null); }} onPerformance={() => { const id = indexChat.id; setIndexChat(null); settings('performance', id); }} key={indexChat.id} chat={indexChat} onClose={() => { setIndexChat(null); void refresh().catch(reportError); }} />}
    {modal === 'sources' && <SettingsDialog titleId="sources-title" onClose={() => { setModal(null); void refresh().catch(reportError); }}>
      <div className="eyebrow">{t('ИСТОЧНИКИ')}</div><h2 id="sources-title">{t('Источники')}</h2>
      <TelegramPanel chats={chats} onChanged={refresh} />
    </SettingsDialog>}
    {modal === 'settings' && <SettingsDialog wide titleId="modal-title" onClose={() => setModal(null)}>
      <h2 id="modal-title">{t('Настройки')}</h2>
      <SettingsCenter section={settingsSection} onSection={setSettingsSection} chats={chats} scope={performanceScope} onScope={setPerformanceScope}
        semantic={semantic} media={media} onSemantic={setSemantic} onMedia={setMedia}
        onRestart={() => { updateObserverRevision.current++; restarting.current = true; setError(''); }} />
    </SettingsDialog>}

    {context && <div className="overlay"><section className="modal context-modal" role="dialog" aria-modal="true" aria-labelledby="context-title">
      <button className="close" aria-label={t("Закрыть контекст")} onClick={closeContext}><Icon name="close" /></button><div className="eyebrow">{t("КОНТЕКСТ ДИАЛОГА")}</div><h2 id="context-title">{context.hit.chat_name}</h2>
      <div className="context-nav"><button disabled={loadingContext} onClick={() => void openContext(context.hit, context.messages[0].message_id)}><Icon name="left" size={16} />{t('Более ранние')}</button><button disabled={loadingContext} onClick={() => void openContext(context.hit, context.messages[context.messages.length - 1].message_id)}>{t('Более поздние')}<Icon name="arrow" size={16} /></button></div>
      <div className="context-messages">{context.messages.map(message => <MessageRow key={message.message_id} message={message} anchor={context.hit.message_id} query={submitted} onOpenImage={setOpenImage} />)}</div>
    </section></div>}
    {openImage && <ImageViewer key={`${openImage.id}/${openImage.messageId}`} image={openImage} onClose={() => setOpenImage(null)} />}
  </div>;
}
