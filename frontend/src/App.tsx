import { getLanguage, t, uiLocale, useLanguage } from './i18n';
import { useEffect, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import { api, initializeSession } from './api';
import ImportDialog from './ImportDialog';
import ConflictDialog from './ConflictDialog';
import ChatIndexDialog from './ChatIndexDialog';
import TelegramPanel from './TelegramPanel';
import SemanticPanel from './SemanticPanel';
import RerankPanel from './RerankPanel';
import WorkspacePanel from './WorkspacePanel';
import UpdatePanel from './UpdatePanel';
import type { Update } from './UpdatePanel';
import SearchSettingsPanel from './SearchSettingsPanel';
import SettingsDialog from './SettingsDialog';
import ImageViewer from './ImageViewer';
import type { OpenImage } from './ImageViewer';
import { useTheme } from './theme';
import './theme.css';
import type { Chat, Hit, Job, MediaStatus, Message, Preview, SearchModality, SemanticStatus } from './types';

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
  rerank_applied?: boolean; limit: number; search_id?: string | null; next_offset?: number | null; cached_results?: number };

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
  const [selected, setSelected] = useState<string[] | null>(null);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [previews, setPreviews] = useState<Preview[]>([]);
  const [activePreview, setActivePreview] = useState<Preview | null>(null);
  const [authors, setAuthors] = useState<{ author_id: string; name: string }[]>([]);
  const [query, setQuery] = useState('');
  const [submitted, setSubmitted] = useState('');
  const [author, setAuthor] = useState('');
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
  const [loadingMore, setLoadingMore] = useState(false);
  const [pagingError, setPagingError] = useState('');
  const searchVersion = useRef(0);
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
  const [diagnostics, setDiagnostics] = useState<Record<string, unknown> | null>(null);
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
  const reportError = (error: unknown) => { if (!restarting.current) setError(error instanceof Error ? error.message : t('Ошибка соединения.')); };
  const refresh = async () => {
    const [chats, jobs, previews, semantic, media] = await Promise.allSettled([api<Chat[]>('/api/chats'), api<Job[]>('/api/imports'), api<Preview[]>('/api/import-previews'), api<SemanticStatus>('/api/semantic'), api<MediaStatus>('/api/media-index')]);
    if (chats.status === 'rejected') throw chats.reason;
    if (jobs.status === 'rejected') throw jobs.reason;
    if (previews.status === 'rejected') throw previews.reason;
    if (semantic.status === 'rejected') throw semantic.reason;
    if (media.status === 'rejected') throw media.reason;
    setChats(chats.value); setJobs(jobs.value); setPreviews(previews.value); setSemantic(semantic.value); setMedia(media.value);
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
    searchVersion.current++; setLoadingMore(false); setSearchId(null); setNextOffset(null);
    setModalities(next); setHits(null); setReranked(false); setWarnings([]); setHasMore(false); closeContext();
  };

  async function search(event: FormEvent) {
    event.preventDefault();
    if (!query.trim() || busy) return;
    if (!modalities.length) { setError(t('Выберите хотя бы один тип поиска.')); return; }
    if (selected?.length === 0) { setError(t('Выберите хотя бы один диалог.')); return; }
    const version = ++searchVersion.current;
    setBusy(true); setError(''); setPagingError(''); setLoadingMore(false); closeContext();
    const params = new URLSearchParams({ q: query, exact: String(exact), content_type: contentType, mode, exclude_deleted: String(excludeDeleted) });
    modalities.forEach(kind => params.append('modality', kind));
    selected?.forEach(id => params.append('chat_id', id));
    if (author) params.set('author_id', author);
    if (from) params.set('date_from', from);
    if (to) params.set('date_to', to);
    try {
      const result = await api<SearchPage>(`/api/search?${params}`);
      if (version !== searchVersion.current) return;
      setSearchId(result.search_id ?? null); setNextOffset(result.next_offset ?? null);
      setHits(result.results); setHasMore(result.has_more); setSubmitted(query);
      setSubmittedLimit(result.limit ?? 20);
      setEffectiveMode(result.effective_mode); setWarnings(result.warnings); setReranked(Boolean(result.rerank_applied));
      setSearchFilters(params.toString());
      setSubmittedModalities([...modalities]);
    } catch (error) { if (version === searchVersion.current) reportError(error); }
    finally { setBusy(false); }
  }

  async function showMore() {
    if (!searchId || nextOffset === null || loadingMore || busy) return;
    const version = searchVersion.current;
    setLoadingMore(true); setPagingError('');
    try {
      const result = await api<SearchPage>(`/api/search/${encodeURIComponent(searchId)}/page?offset=${nextOffset}&limit=${submittedLimit}`);
      if (version !== searchVersion.current) return;
      setHits(current => [...(current ?? []), ...result.results]);
      setNextOffset(result.next_offset ?? null); setHasMore(result.has_more);
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
      searchVersion.current++; setSearchId(null); setNextOffset(null); setLoadingMore(false); setPagingError('');
      setSelected(null); setHits(null); setReranked(false); closeContext(); await refresh();
    } catch (error) { reportError(error); }
    finally { setDeleting(false); }
  }

  async function settings() {
    setModal('settings');
    try { setDiagnostics(await api('/api/doctor')); }
    catch (error) { reportError(error); }
  }

  const messageCount = chats.reduce((sum, chat) => sum + chat.messages, 0);
  const photoCount = chats.reduce((sum, chat) => sum + chat.photos, 0);
  const onlyImages = submittedModalities.length === 1 && submittedModalities[0] === 'images';
  return <div className="app">
    <aside className="sidebar">
      <a href="/" className="brand"><span className="brand-icon" aria-hidden="true">↗</span>
        <span>{t("Архив")}<small>TELEGRAM SEARCH</small></span></a>
      <div className="sidebar-content">
      <div className="sidebar-title"><span>{t("Диалоги")}</span><button className="text-button" onClick={() => setSelected(null)}>{t("Все")}</button></div>
      <div className="chat-list">
        {chats.map(chat => <div className="chat-item" key={chat.id}>
          <label><input type="checkbox" checked={selected === null || selected.includes(chat.id)} onChange={() => toggleChat(chat.id)} />
            <span><strong>{chat.name}</strong><small>{chat.messages.toLocaleString(uiLocale())}{t(" сообщений · ")}{chat.photos}{t(" фото")}</small></span></label>
          <button className="chat-settings-button" aria-label={t('Настройки индексации {p0}', { p0: chat.name })} title={t('Настройки индексации')} onClick={() => setIndexChat(chat)}>⋯</button>
          <button className="delete-button" disabled={deleting} aria-label={t("Удалить {p0}", { p0: chat.name })} onClick={() => void deleteChat(chat)}>×</button>
        </div>)}
        {!chats.length && <p className="sidebar-empty">{t("Добавьте экспорт, чтобы ваша переписка стала доступна для поиска.")}</p>}
      </div>
      <button className="import-button telegram-sources-button" disabled={!connected} onClick={() => setModal('sources')}>{t('Источники · Telegram')}</button>
      <button className="import-button" disabled={!connected} onClick={() => { setActivePreview(null); setModal('import'); }}><span>＋</span>{t(" Импортировать выгрузку")}</button>
      {previews.length > 0 && <div className="jobs"><div className="sidebar-title">{t("Проверки экспорта")}</div>
        {previews.map(preview => <div className="job" key={preview.id}><small><strong>{preview.chat_name}</strong> · {preview.scope}</small><small>{preview.processed}{t(" проверено · ")}{preview.state === 'ready' ? t('отчёт готов') : t(stateNames[preview.state] || preview.state)}</small><div className="job-actions"><button onClick={() => { setActivePreview(preview); setModal('import'); }}>{t("Открыть отчёт")}</button></div></div>)}
      </div>}
      {jobs.length > 0 && <div className="jobs"><div className="sidebar-title">{t("Последние импорты")}</div>
        {jobs.slice(0, 4).map(job => <div className="job" key={job.id}>
          <div className={`job-state ${job.state}`}><span className="dot" />{t(stateNames[job.state] || job.state)}</div>
          <small>{job.processed}{t(" обработано · ")}{job.added}{t(" новых · ")}{job.updated}{t(" обновлено")}</small>
          {job.pending_conflicts > 0 && <small className="warning">{t("Неразрешённых конфликтов: ")}{job.pending_conflicts}</small>}
          {(job.missing_media + job.invalid_media) > 0 && <small className="warning">{t("Недоступных вложений: ")}{job.missing_media + job.invalid_media}</small>}
          {job.error && <small className="warning">{t(job.error)}</small>}
          {job.warnings?.map(warning => <small className="warning" key={warning}>{t(warning)}</small>)}
          <div className="job-actions">
            {job.pending_conflicts > 0 && <button onClick={() => setConflictJob(job)}>{t("Разобрать конфликты")}</button>}
            {['queued', 'running'].includes(job.state) && <button onClick={() => void control(job, 'pause')}>{t("Пауза")}</button>}
            {['paused', 'interrupted', 'failed'].includes(job.state) && <button onClick={() => void control(job, 'resume')}>{t("Продолжить")}</button>}
            {['running', 'queued', 'paused', 'interrupted'].includes(job.state) && <button onClick={() => void control(job, 'cancel')}>{t("Отменить")}</button>}
          </div>
        </div>)}
      </div>}
      </div>
      <div className="sidebar-footer"><button className="settings-button" onClick={() => void settings()}><span aria-hidden="true">⚙</span>{t('Настройки')}</button><span><i className="dot" />{t("Локально на этом компьютере")}</span></div>
    </aside>

    <main className="main">
      <header className="topbar"><span>{t("Ваша переписка. Под рукой.")}</span><div className="topbar-actions">
        <button className="theme-toggle mobile-settings-button" type="button" aria-label={t('Настройки')} title={t('Настройки')} onClick={() => void settings()}><span aria-hidden="true">⚙</span></button>
        <button className="theme-toggle" type="button" aria-label={t('Тёмная тема')} aria-pressed={theme === 'dark'} title={t('Тёмная тема')} onClick={toggleTheme}><span aria-hidden="true">{theme === 'dark' ? '☀' : '☾'}</span></button>
        <label className="language-switch"><span className={language === 'ru' ? 'active' : ''}>RU</span>
          <input type="range" min="0" max="1" step="1" value={language === 'en' ? 1 : 0}
            aria-label={t('Язык приложения')} aria-valuetext={language === 'en' ? 'English' : 'Русский'}
            onChange={event => changeLanguage(event.target.value === '1' ? 'en' : 'ru')} />
          <span className={language === 'en' ? 'active' : ''}>EN</span>
        </label><span className="pill">{t('Индекс: {p0} · Поиск: {p1}', { p0: indexingDevices, p1: searchDevices })} <span className="dot" /></span>
      </div></header>
      <div className="content">
        <div className="heading"><div className="eyebrow">{t("ЛИЧНЫЙ АРХИВ")}</div><h1>{t("Найдите тот самый разговор.")}</h1>
          <p>{t("Слова, фразы и фотографии из ваших диалогов — в одном месте.")}</p></div>
        {error && <div className="error" role="alert"><span>{t(error)}</span><button aria-label={t("Закрыть ошибку")} onClick={() => setError('')}>×</button></div>}
        <form onSubmit={search} className="search-form">
          <div className="search-box"><span className="search-icon" aria-hidden="true">⌕</span>
            <input aria-label={t("Поисковый запрос")} placeholder={t("Что вы хотите найти в переписке?")} value={query} onChange={event => setQuery(event.target.value)} />
            <button disabled={busy || !query.trim() || !connected || !modalities.length}>{busy ? t('Ищем…') : t('Найти')}<span aria-hidden="true"> ↗</span></button></div>
          <div className="filters">
            <label>{t("Режим")}<select aria-label={t("Режим поиска")} value={mode} disabled={exact} onChange={event => setMode(event.target.value)}><option value="words">{t("По словам")}</option><option value="meaning">{t("По смыслу")}</option><option value="hybrid">{t("Слова и смысл")}</option></select></label>
            <label>{t("Автор")}<select aria-label={t("Автор")} value={author} onChange={event => setAuthor(event.target.value)}><option value="">{t("Все авторы")}</option>{authors.map(item => <option value={item.author_id} key={item.author_id}>{item.name || item.author_id}</option>)}</select></label>
            <label>{t("С даты (UTC)")}<input type="date" value={from} onChange={event => setFrom(event.target.value)} /></label>
            <label>{t("По дату (UTC)")}<input type="date" value={to} onChange={event => setTo(event.target.value)} /></label>
            <label>{t("Содержимое")}<select value={contentType} onChange={event => setContentType(event.target.value)}><option value="all">{t("Все сообщения")}</option><option value="text">{t("С текстом")}</option><option value="photo">{t("С фотографией")}</option></select></label>
          </div>
          <div className="search-options"><label><input type="checkbox" checked={exact} onChange={event => setExact(event.target.checked)} />{t("Точная фраза")}</label>
            <label><input type="checkbox" checked={excludeDeleted} onChange={event => setExcludeDeleted(event.target.checked)} />{t('Скрыть удалённые в Telegram')}</label>
            <div className="search-option-actions">
              <button type="button" className="text-button" aria-expanded={showSearchSettings} aria-controls="search-display-settings" onClick={() => setShowSearchSettings(value => !value)}><span aria-hidden="true">{showSearchSettings ? '▾' : '▸'}</span> <span>{t('Выдача поиска')}</span></button>
              <button type="button" className="text-button" onClick={() => { setAuthor(''); setFrom(''); setTo(''); setContentType('all'); setExact(false); setExcludeDeleted(false); setSelected(null); }}>{t("Сбросить фильтры")}</button>
            </div></div>
          <fieldset className="search-modalities" disabled={busy} aria-describedby="search-modality-help">
            <legend>{t("Искать в")}</legend>
            <div className="modality-options">
              <button type="button" aria-pressed={modalities.length === allModalities.length} onClick={() => changeModalities([...allModalities])}>{t("Всё")}</button>
              {allModalities.map(kind => <label key={kind} className={modalities.includes(kind) ? 'selected' : ''}>
                <input type="checkbox" checked={modalities.includes(kind)} onChange={() => changeModalities(allModalities.filter(value => value === kind ? !modalities.includes(kind) : modalities.includes(value)))} />
                {t(modalityLabels[kind])}
              </label>)}
            </div>
            <p id="search-modality-help" className="baseline-note">{t("Можно выбрать несколько типов поиска — результаты объединятся.")}</p>
            {!modalities.length && <p className="warning" role="status">{t("Выберите хотя бы один тип поиска.")}</p>}
          </fieldset>
        </form>
        <section id="search-display-settings" className="search-display-options" aria-label={t('Выдача поиска')} hidden={!showSearchSettings}><SearchSettingsPanel /></section>
        {busy && <div className="search-progress" role="status"><span>{t('Ищем…')}</span><progress aria-label={t('Выполнение поиска')} /></div>}
        {exact && <p className="baseline-note">{t("Точная фраза ищется в сообщениях и распознанном тексте фотографий.")}</p>}
        {semantic?.enabled === 1 && <p className="baseline-note">{t("Смысловой индекс: ")}{semantic.ready_segments} / {semantic.total_segments}{t(" сегментов")}{semantic.paused ? t(' · на паузе') : ''}</p>}
        {media && (media.ocr_enabled === 1 || media.images_enabled === 1) && <p className="baseline-note">{t("Фотографии: ")}{media.images_ready} / {media.total_photos} · OCR: {media.ocr_ready} / {media.total_photos}{t(" · OCR по смыслу: ")}{media.ocr_dense_ready}{media.paused ? t(' · медиа на паузе') : ''}</p>}
        {reranked && hits !== null && !busy && <p className="baseline-note" role="status">{t('Порядок текста и OCR уточнён Giga')}</p>}
        {warnings.map(warning => <p className="warning" role="status" key={warning}>{t(warning)}</p>)}

        {hits === null ? <section className="welcome">
          <div className="archive-symbol" aria-hidden="true">▤</div><h2>{t("Разговоры остаются рядом.")}</h2>
          <p>{chats.length ? t('Введите слово или фразу. Откройте результат, чтобы увидеть сообщения до и после совпадения.') : t('Начните с JSON-экспорта Telegram Desktop. Мы прочитаем сообщения и свяжем фотографии с вашей папкой.')}</p>
          <div className="stats"><div><strong>{messageCount.toLocaleString(uiLocale())}</strong><span>{t("сообщений")}</span></div><div><strong>{chats.length}</strong><span>{t("диалогов")}</span></div><div><strong>{photoCount}</strong><span>{t("фотографий")}</span></div></div>
          <div className="baseline-note">{t('Слова и смысл — базовый режим. Подготовьте модели в общих настройках.')}</div>
        </section> : <section className="results" aria-live="polite">
          <div className="results-heading"><h2>{hits.length ? t("Найдено фрагментов: {p0}{p1}", { p0: hits.length, p1: hasMore ? '+' : '' }) : t('Совпадений пока нет')}</h2><span>{onlyImages ? t('По описанию · изображения') : effectiveMode === 'mixed' ? t("{p0} · общая выдача", { p0: submittedModalities.map(kind => t(modalityLabels[kind])).join(' + ') }) : effectiveMode === 'hybrid' ? t('Слова и смысл · RRF') : effectiveMode === 'meaning' ? t('По смыслу · векторы') : t('По словам · BM25')}</span></div>
          {!hits.length && <div className="no-results">{t("Попробуйте другой запрос или расширьте область поиска.")}{onlyImages ? t(' Проверьте готовность индекса фотографий.') : effectiveMode === 'words' ? t(' Поиск по словам требует все слова запроса.') : t(' Проверьте готовность выбранных индексов.')}</div>}
          <div className={onlyImages ? 'photo-grid' : 'result-list'}>{hits.map((hit, index) => <article className="result-card" key={hit.chunk_id || `${hit.chat_id}/${hit.message_id}`}>
            <div className="result-header"><span><span className="chat-badge" aria-hidden="true">▤</span>{hit.chat_name}</span><small>{hit.chunk_id ? t('Опорное сообщение фрагмента') : t('Совпадение в')} #{hit.message_id}</small></div>
            <div className="result-ranking"><span>{t('Результат #{p0}', { p0: index + 1 })}</span>
              {hit.image_similarity != null && Number.isFinite(hit.image_similarity) && <span title={t('Сходство изображения с описанием: от −1 до 1. Чем выше, тем ближе совпадение; это не вероятность.')}>
                {t('Сходство изображения: {p0}', { p0: hit.image_similarity.toLocaleString(uiLocale(), { minimumFractionDigits: 4, maximumFractionDigits: 4 }) })}
              </span>}
            </div>
            {hit.matched_by && <div className="match-reasons">{hit.matched_by.map(reason => t(reasons[reason])).join(' · ')}</div>}
            {hit.messages.map(message => <MessageRow key={message.message_id} message={message} anchor={hit.message_id} query={submitted} onOpenImage={setOpenImage} />)}
            {hit.matched_parts?.some(part => !hit.messages.some(message => message.message_id === part.message_id)) && <p className="baseline-note">{t("Показана часть найденного фрагмента. Другие сообщения доступны через «Открыть контекст».")}</p>}
            {hit.ocr_match && hit.ocr_match.kind !== 'exact' && <p className="baseline-note ocr-match">{hit.ocr_match.kind === 'substring' ? t('OCR: совпала часть слова') : t('OCR: неточное совпадение · отличий: {p0}', { p0: hit.ocr_match.edits })}</p>}
            {hit.ocr_text && <details className="ocr-evidence"><summary>{t("Распознанный текст")}{hit.ocr_confidence != null ? t(" · уверенность OCR {p0} / 100", { p0: Math.round(hit.ocr_confidence) }) : ''}</summary><div className="message-text"><Highlight text={hit.ocr_text} query={submitted} /></div><p>{t("Распознавание может содержать ошибки. Откройте фотографию для проверки.")}</p></details>}
            <button className="context-button" disabled={loadingContext} onClick={() => void openContext(hit)}>{t("Открыть контекст ")}<span>↗</span></button>
          </article>)}</div>
          {nextOffset !== null && <div className="more-results"><button type="button" className="primary" disabled={busy || loadingMore} onClick={() => void showMore()}>{loadingMore ? t('Загружаем…') : t('Показать ещё')}</button><span className="baseline-note">{t('Следующие результаты загружаются из кэша поиска.')}</span></div>}
          {pagingError && <p className="error" role="alert">{t(pagingError)}</p>}
          {hasMore && nextOffset === null && <p className="more-note">{t('Показаны первые {p0} фрагментов. Уточните запрос или фильтры, чтобы найти больше.', { p0: hits.length })}</p>}
        </section>}
      </div><footer className="main-footer">{t("Сообщения хранятся и обрабатываются на этом компьютере.")}</footer>
    </main>

    {modal === 'import' && <div className="overlay"><ImportDialog chats={chats} initialPreview={activePreview} onClose={() => setModal(null)} onApplied={refresh} /></div>}
    {conflictJob && <div className="overlay"><ConflictDialog job={conflictJob} onClose={() => setConflictJob(null)} onChanged={refresh} /></div>}
    {indexChat && <ChatIndexDialog onSources={() => { setIndexChat(null); setModal('sources'); }} onModels={() => { setIndexChat(null); void settings(); }} key={indexChat.id} chat={indexChat} onClose={() => { setIndexChat(null); void refresh().catch(reportError); }} />}
    {modal === 'sources' && <SettingsDialog titleId="sources-title" onClose={() => { setModal(null); void refresh().catch(reportError); }}>
      <div className="eyebrow">{t('ИСТОЧНИКИ')}</div><h2 id="sources-title">{t('Источники')}</h2>
      <TelegramPanel chats={chats} onChanged={refresh} />
    </SettingsDialog>}
    {modal === 'settings' && <SettingsDialog titleId="modal-title" onClose={() => setModal(null)}>
      <div className="eyebrow">{t("ЭТОТ КОМПЬЮТЕР")}</div><h2 id="modal-title">{t("Настройки и диагностика")}</h2><p>{t("Обработка сообщений проходит локально на выбранном устройстве.")}</p>
      {diagnostics ? <dl className="diagnostics"><dt>{t("База")}</dt><dd>{diagnostics.database_check === 'ok' ? t('Исправна') : t('Требует проверки')}</dd><dt>{t("Сообщений")}</dt><dd>{String(diagnostics.messages)}</dd><dt>{t("Сегменты в очереди индекса")}</dt><dd>{String(diagnostics.pending_index_segments)}</dd><dt>{t("Доступно памяти")}</dt><dd>{(Number(diagnostics.ram_available_bytes) / 1024 ** 3).toFixed(1)}{t(" ГиБ")}</dd><dt>{t("Свободно на диске")}</dt><dd>{(Number(diagnostics.disk_free_bytes) / 1024 ** 3).toFixed(1)}{t(" ГиБ")}</dd></dl> : <p>{t("Проверяем…")}</p>}
      <p className="baseline-note">{t("База хранится локально без шифрования.")}</p>
      <SemanticPanel status={semantic} onChange={setSemantic} />
      <RerankPanel />
      <WorkspacePanel media={media} onMediaChange={setMedia} />
      <UpdatePanel onRestart={() => { updateObserverRevision.current++; restarting.current = true; setError(''); }} />
    </SettingsDialog>}

    {context && <div className="overlay"><section className="modal context-modal" role="dialog" aria-modal="true" aria-labelledby="context-title">
      <button className="close" aria-label={t("Закрыть контекст")} onClick={closeContext}>×</button><div className="eyebrow">{t("КОНТЕКСТ ДИАЛОГА")}</div><h2 id="context-title">{context.hit.chat_name}</h2>
      <div className="context-nav"><button disabled={loadingContext} onClick={() => void openContext(context.hit, context.messages[0].message_id)}>{t("← Более ранние")}</button><button disabled={loadingContext} onClick={() => void openContext(context.hit, context.messages[context.messages.length - 1].message_id)}>{t("Более поздние →")}</button></div>
      <div className="context-messages">{context.messages.map(message => <MessageRow key={message.message_id} message={message} anchor={context.hit.message_id} query={submitted} onOpenImage={setOpenImage} />)}</div>
    </section></div>}
    {openImage && <ImageViewer key={`${openImage.id}/${openImage.messageId}`} image={openImage} onClose={() => setOpenImage(null)} />}
  </div>;
}
