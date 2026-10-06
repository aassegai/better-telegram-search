import { useEffect, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import { api, initializeSession } from './api';
import ImportDialog from './ImportDialog';
import ConflictDialog from './ConflictDialog';
import SemanticPanel from './SemanticPanel';
import WorkspacePanel from './WorkspacePanel';
import type { Chat, Hit, Job, MediaStatus, Message, Preview, SemanticStatus } from './types';

const dates = new Intl.DateTimeFormat('ru-RU', { dateStyle: 'medium', timeStyle: 'short' });
const date = (value: number) => dates.format(new Date(value * 1000));
const normalize = (text: string) => text.normalize('NFKC').toLocaleLowerCase('ru').replaceAll('ё', 'е');
const stateNames: Record<string, string> = {
  queued: 'В очереди', running: 'Импортируется', completed: 'Поиск доступен',
  paused: 'Приостановлено', interrupted: 'Прервано', cancelled: 'Отменено', failed: 'Ошибка',
};
const reasons: Record<string, string> = { words: 'Совпали слова', meaning: 'Близкий смысл', image: 'Фотография по описанию', ocr_words: 'Слова на фотографии', ocr_meaning: 'Смысл текста на фотографии' };

function Highlight({ text, query }: { text: string; query: string }) {
  const words = new Set(normalize(query).match(/[\p{L}\p{N}]+/gu) || []);
  return <>{text.split(/([\p{L}\p{N}]+)/u).map((part, i) =>
    words.has(normalize(part)) ? <mark key={i}>{part}</mark> : part)}</>;
}

function MessageRow({ message, anchor, query = '' }: { message: Message; anchor: number; query?: string }) {
  const photo = message.media.find(media => media.kind === 'photo');
  const [failed, setFailed] = useState(false);
  return <div className={`message ${message.message_id === anchor ? 'anchor' : ''}`}>
    <div className="message-meta"><strong>{message.author || 'Служебное событие'}</strong>
      <time>{date(message.timestamp)}</time>
      {!message.matches_filters && <span className="context-tag">вне фильтра · контекст</span>}
    </div>
    {message.forwarded_from && <div className="message-note">Переслано: {message.forwarded_from}</div>}
    {message.reply_to && <div className="message-note">Ответ на #{message.reply_to}</div>}
    <div className="message-text"><Highlight text={message.text || message.action || 'Сообщение без текста'} query={query} /></div>
    {photo && (photo.status === 'ready' && !failed ?
      <a className="photo-link" href={`/api/media/${photo.id}`} target="_blank" rel="noreferrer">
        <img loading="lazy" src={`/api/media/${photo.id}`} alt="Фотография из сообщения" onError={() => setFailed(true)} />
      </a> : <div className="missing-photo">Изображение недоступно в папке источника</div>)}
    {message.media.some(media => media.kind === 'attachment') && <div className="message-note">В исходном экспорте есть вложение</div>}
  </div>;
}

export default function App() {
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
  const [mode, setMode] = useState('words');
  const [effectiveMode, setEffectiveMode] = useState('words');
  const [warnings, setWarnings] = useState<string[]>([]);
  const [semantic, setSemantic] = useState<SemanticStatus | null>(null);
  const [media, setMedia] = useState<MediaStatus | null>(null);
  const [tab, setTab] = useState('all');
  const [submittedTab, setSubmittedTab] = useState('all');
  const [deleting, setDeleting] = useState(false);
  const [hits, setHits] = useState<Hit[] | null>(null);
  const [hasMore, setHasMore] = useState(false);
  const [busy, setBusy] = useState(false);
  const [connected, setConnected] = useState(false);
  const [error, setError] = useState('');
  const [modal, setModal] = useState<'import' | 'settings' | null>(null);
  const [conflictJob, setConflictJob] = useState<Job | null>(null);
  const [context, setContext] = useState<{ hit: Hit; messages: Message[] } | null>(null);
  const [loadingContext, setLoadingContext] = useState(false);
  const [diagnostics, setDiagnostics] = useState<Record<string, unknown> | null>(null);
  const authorsVersion = jobs.map(job => `${job.id}:${job.processed}:${job.state}:${job.pending_conflicts}`).join('|');
  const contextVersion = useRef(0);
  const [searchFilters, setSearchFilters] = useState('');
  const closeContext = () => { contextVersion.current++; setContext(null); setLoadingContext(false); };

  const reportError = (error: unknown) => setError(error instanceof Error ? error.message : 'Ошибка соединения.');
  const refresh = async () => {
    const [chats, jobs, previews, semantic, media] = await Promise.all([api<Chat[]>('/api/chats'), api<Job[]>('/api/imports'), api<Preview[]>('/api/import-previews'), api<SemanticStatus>('/api/semantic'), api<MediaStatus>('/api/media-index')]);
    setChats(chats); setJobs(jobs); setPreviews(previews); setSemantic(semantic); setMedia(media);
  };

  useEffect(() => {
    let alive = true;
    initializeSession().then(() => {
      if (alive) { setConnected(true); void refresh().catch(reportError); }
    }).catch(reportError);
    const timer = window.setInterval(() => { if (alive) void refresh().catch(reportError); }, 2000);
    return () => { alive = false; clearInterval(timer); };
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
      if (event.key === 'Escape') { setModal(null); setConflictJob(null); closeContext(); }
    };
    window.addEventListener('keydown', escape);
    return () => window.removeEventListener('keydown', escape);
  }, []);

  const toggleChat = (id: string) => setSelected(current => {
    const chosen = current ?? chats.map(chat => chat.id);
    return chosen.includes(id) ? chosen.filter(value => value !== id) : [...chosen, id];
  });

  async function search(event: FormEvent) {
    event.preventDefault();
    if (!query.trim() || busy) return;
    if (selected?.length === 0) { setError('Выберите хотя бы один диалог.'); return; }
    setBusy(true); setError('');
    const params = new URLSearchParams({ q: query, exact: String(exact), content_type: contentType, mode, tab });
    selected?.forEach(id => params.append('chat_id', id));
    if (author) params.set('author_id', author);
    if (from) params.set('date_from', from);
    if (to) params.set('date_to', to);
    try {
      const result = await api<{ results: Hit[]; has_more: boolean; effective_mode: string; warnings: string[] }>(`/api/search?${params}`);
      setHits(result.results); setHasMore(result.has_more); setSubmitted(query);
      setEffectiveMode(result.effective_mode); setWarnings(result.warnings);
      setSearchFilters(params.toString());
      setSubmittedTab(tab);
    } catch (error) { reportError(error); }
    finally { setBusy(false); }
  }

  async function openContext(hit: Hit, anchor = hit.message_id) {
    const version = ++contextVersion.current;
    setLoadingContext(true);
    try {
      const result = await api<{ messages: Message[] }>(`/api/chats/${hit.chat_id}/context/${anchor}?before=15&after=15&${searchFilters}`);
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
      if (!window.confirm(`Удалить «${chat.name}» и его индекс из приложения? ${estimate.messages} сообщений, ${(estimate.message_text_bytes / 1024).toFixed(1)} КиБ текста. Кэш ${estimate.exclusive_media} вложений удалится; общих вложений ${estimate.shared_media}. Точное освобождение места зависит от уплотнения индекса. Исходный экспорт сохранится.`)) return;
      await api(`/api/chats/${chat.id}`, { method: 'DELETE' });
      setSelected(null); setHits(null); await refresh();
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
  return <div className="app">
    <aside className="sidebar">
      <a href="/" className="brand"><span className="brand-icon" aria-hidden="true">↗</span>
        <span>Архив<small>TELEGRAM SEARCH</small></span></a>
      <div className="sidebar-title"><span>Диалоги</span><button className="text-button" onClick={() => setSelected(null)}>Все</button></div>
      <div className="chat-list">
        {chats.map(chat => <div className="chat-item" key={chat.id}>
          <label><input type="checkbox" checked={selected === null || selected.includes(chat.id)} onChange={() => toggleChat(chat.id)} />
            <span><strong>{chat.name}</strong><small>{chat.messages.toLocaleString('ru-RU')} сообщений · {chat.photos} фото</small></span></label>
          <button className="delete-button" disabled={deleting} aria-label={`Удалить ${chat.name}`} onClick={() => void deleteChat(chat)}>×</button>
        </div>)}
        {!chats.length && <p className="sidebar-empty">Добавьте экспорт, чтобы ваша переписка стала доступна для поиска.</p>}
      </div>
      <button className="import-button" disabled={!connected} onClick={() => { setActivePreview(null); setModal('import'); }}><span>＋</span> Импортировать экспорт</button>
      {previews.length > 0 && <div className="jobs"><div className="sidebar-title">Проверки экспорта</div>
        {previews.map(preview => <div className="job" key={preview.id}><small><strong>{preview.chat_name}</strong> · {preview.scope}</small><small>{preview.processed} проверено · {preview.state === 'ready' ? 'отчёт готов' : stateNames[preview.state] || preview.state}</small><div className="job-actions"><button onClick={() => { setActivePreview(preview); setModal('import'); }}>Открыть отчёт</button></div></div>)}
      </div>}
      {jobs.length > 0 && <div className="jobs"><div className="sidebar-title">Последние импорты</div>
        {jobs.slice(0, 4).map(job => <div className="job" key={job.id}>
          <div className={`job-state ${job.state}`}><span className="dot" />{stateNames[job.state] || job.state}</div>
          <small>{job.processed} обработано · {job.added} новых · {job.updated} обновлено</small>
          {job.pending_conflicts > 0 && <small className="warning">Неразрешённых конфликтов: {job.pending_conflicts}</small>}
          {(job.missing_media + job.invalid_media) > 0 && <small className="warning">Недоступных вложений: {job.missing_media + job.invalid_media}</small>}
          {job.error && <small className="warning">{job.error}</small>}
          {job.warnings?.map(warning => <small className="warning" key={warning}>{warning}</small>)}
          <div className="job-actions">
            {job.pending_conflicts > 0 && <button onClick={() => setConflictJob(job)}>Разобрать конфликты</button>}
            {['queued', 'running'].includes(job.state) && <button onClick={() => void control(job, 'pause')}>Пауза</button>}
            {['paused', 'interrupted', 'failed'].includes(job.state) && <button onClick={() => void control(job, 'resume')}>Продолжить</button>}
            {['running', 'queued', 'paused', 'interrupted'].includes(job.state) && <button onClick={() => void control(job, 'cancel')}>Отменить</button>}
          </div>
        </div>)}
      </div>}
      <div className="sidebar-footer"><button onClick={() => void settings()}>⚙ Настройки и диагностика</button><span><i className="dot" />Локально на этом компьютере</span></div>
    </aside>

    <main className="main">
      <header className="topbar"><span>Ваша переписка. Под рукой.</span><span className="pill">CPU <span className="dot" /></span></header>
      <div className="content">
        <div className="heading"><div className="eyebrow">ЛИЧНЫЙ АРХИВ</div><h1>Найдите тот самый разговор.</h1>
          <p>Слова, фразы и фотографии из ваших диалогов — в одном месте.</p></div>
        {error && <div className="error" role="alert"><span>{error}</span><button aria-label="Закрыть ошибку" onClick={() => setError('')}>×</button></div>}
        <form onSubmit={search} className="search-form">
          <div className="search-box"><span className="search-icon" aria-hidden="true">⌕</span>
            <input aria-label="Поисковый запрос" placeholder="Что вы хотите найти в переписке?" value={query} onChange={event => setQuery(event.target.value)} />
            <button disabled={busy || !query.trim() || !connected}>{busy ? 'Ищем…' : 'Найти'}<span aria-hidden="true"> ↗</span></button></div>
          <div className="filters">
            <label>Режим<select aria-label="Режим поиска" value={mode} disabled={exact} onChange={event => setMode(event.target.value)}><option value="words">По словам</option><option value="meaning">По смыслу</option><option value="hybrid">Слова и смысл</option></select></label>
            <label>Автор<select aria-label="Автор" value={author} onChange={event => setAuthor(event.target.value)}><option value="">Все авторы</option>{authors.map(item => <option value={item.author_id} key={item.author_id}>{item.name || item.author_id}</option>)}</select></label>
            <label>С даты (UTC)<input type="date" value={from} onChange={event => setFrom(event.target.value)} /></label>
            <label>По дату (UTC)<input type="date" value={to} onChange={event => setTo(event.target.value)} /></label>
            <label>Содержимое<select value={contentType} onChange={event => setContentType(event.target.value)}><option value="all">Все сообщения</option><option value="text">С текстом</option><option value="photo">С фотографией</option></select></label>
          </div>
          <div className="search-options"><label><input type="checkbox" checked={exact} onChange={event => setExact(event.target.checked)} />Точная фраза</label>
            <button type="button" className="text-button" onClick={() => { setAuthor(''); setFrom(''); setTo(''); setContentType('all'); setExact(false); setSelected(null); }}>Сбросить фильтры</button></div>
        </form>
        <div className="search-tabs" role="tablist" aria-label="Раздел поиска">{[['all', 'Всё'], ['text', 'Текст'], ['images', 'Изображения'], ['ocr', 'OCR']].map(([key, label]) => <button type="button" role="tab" key={key} aria-selected={tab === key} disabled={busy} onClick={() => { setTab(key); setHits(null); setWarnings([]); }}>{label}</button>)}</div>
        {exact && <p className="baseline-note">Точная фраза ищется в сообщениях и распознанном тексте фотографий.</p>}
        {semantic?.enabled === 1 && <p className="baseline-note">Смысловой индекс: {semantic.ready_segments} / {semantic.total_segments} сегментов{semantic.paused ? ' · на паузе' : ''}</p>}
        {media && (media.ocr_enabled === 1 || media.images_enabled === 1) && <p className="baseline-note">Фотографии: {media.images_ready} / {media.total_photos} · OCR: {media.ocr_ready} / {media.total_photos} · OCR по смыслу: {media.ocr_dense_ready}{media.paused ? ' · медиа на паузе' : ''}</p>}
        {warnings.map(warning => <p className="warning" role="status" key={warning}>{warning}</p>)}

        {hits === null ? <section className="welcome">
          <div className="archive-symbol" aria-hidden="true">▤</div><h2>Разговоры остаются рядом.</h2>
          <p>{chats.length ? 'Введите слово или фразу. Откройте результат, чтобы увидеть сообщения до и после совпадения.' : 'Начните с JSON-экспорта Telegram Desktop. Мы прочитаем сообщения и свяжем фотографии с вашей папкой.'}</p>
          <div className="stats"><div><strong>{messageCount.toLocaleString('ru-RU')}</strong><span>сообщений</span></div><div><strong>{chats.length}</strong><span>диалогов</span></div><div><strong>{photoCount}</strong><span>фотографий</span></div></div>
          <div className="baseline-note">Поиск по словам доступен сразу. Для поиска по смыслу подготовьте модель в настройках.</div>
        </section> : <section className="results" aria-live="polite">
          <div className="results-heading"><h2>{hits.length ? `Найдено фрагментов: ${hits.length}${hasMore ? '+' : ''}` : 'Совпадений пока нет'}</h2><span>{submittedTab === 'images' ? 'По описанию · CLIP' : effectiveMode === 'mixed' ? 'Общая выдача · RRF' : effectiveMode === 'hybrid' ? 'Слова и смысл · RRF' : effectiveMode === 'meaning' ? 'По смыслу · E5' : 'По словам · BM25'}</span></div>
          {!hits.length && <div className="no-results">Попробуйте другой запрос или расширьте область поиска.{submittedTab === 'images' ? ' Проверьте готовность индекса фотографий.' : effectiveMode === 'words' ? ' Поиск по словам требует все слова запроса.' : ' Проверьте готовность выбранных индексов.'}</div>}
          <div className={submittedTab === 'images' ? 'photo-grid' : 'result-list'}>{hits.map(hit => <article className="result-card" key={hit.chunk_id || `${hit.chat_id}/${hit.message_id}`}>
            <div className="result-header"><span><span className="chat-badge" aria-hidden="true">▤</span>{hit.chat_name}</span><small>{hit.chunk_id ? 'Опорное сообщение фрагмента' : 'Совпадение в'} #{hit.message_id}</small></div>
            {hit.matched_by && <div className="match-reasons">{hit.matched_by.map(reason => reasons[reason]).join(' · ')}</div>}
            {hit.messages.map(message => <MessageRow key={message.message_id} message={message} anchor={hit.message_id} query={submitted} />)}
            {hit.ocr_text && <details className="ocr-evidence"><summary>Распознанный текст{hit.ocr_confidence != null ? ` · уверенность OCR ${Math.round(hit.ocr_confidence)} / 100` : ''}</summary><div className="message-text"><Highlight text={hit.ocr_text} query={submitted} /></div><p>Распознавание может содержать ошибки. Откройте фотографию для проверки.</p></details>}
            <button className="context-button" disabled={loadingContext} onClick={() => void openContext(hit)}>Открыть контекст <span>↗</span></button>
          </article>)}</div>
          {hasMore && <p className="more-note">Показаны первые 20 фрагментов. Уточните запрос или фильтры.</p>}
        </section>}
      </div><footer className="main-footer">Сообщения хранятся и обрабатываются на этом компьютере.</footer>
    </main>

    {modal === 'import' && <div className="overlay"><ImportDialog chats={chats} initialPreview={activePreview} onClose={() => setModal(null)} onApplied={refresh} /></div>}
    {conflictJob && <div className="overlay"><ConflictDialog job={conflictJob} onClose={() => setConflictJob(null)} onChanged={refresh} /></div>}
    {modal === 'settings' && <div className="overlay"><section className="modal" role="dialog" aria-modal="true" aria-labelledby="modal-title">
      <button className="close" aria-label="Закрыть" onClick={() => setModal(null)}>×</button>
      <div className="eyebrow">ЭТОТ КОМПЬЮТЕР</div><h2 id="modal-title">Настройки и диагностика</h2><p>Приложение использует только CPU.</p>
      {diagnostics ? <dl className="diagnostics"><dt>Устройство</dt><dd>CPU</dd><dt>База</dt><dd>{diagnostics.database_check === 'ok' ? 'Исправна' : 'Требует проверки'}</dd><dt>Сообщений</dt><dd>{String(diagnostics.messages)}</dd><dt>Сегменты в очереди индекса</dt><dd>{String(diagnostics.pending_index_segments)}</dd><dt>Доступно памяти</dt><dd>{(Number(diagnostics.ram_available_bytes) / 1024 ** 3).toFixed(1)} ГиБ</dd><dt>Свободно на диске</dt><dd>{(Number(diagnostics.disk_free_bytes) / 1024 ** 3).toFixed(1)} ГиБ</dd></dl> : <p>Проверяем…</p>}
      <p className="baseline-note">База хранится локально без шифрования.</p>
      <SemanticPanel status={semantic} onChange={setSemantic} />
      <WorkspacePanel media={media} onMediaChange={setMedia} />
    </section></div>}

    {context && <div className="overlay"><section className="modal context-modal" role="dialog" aria-modal="true" aria-labelledby="context-title">
      <button className="close" aria-label="Закрыть контекст" onClick={closeContext}>×</button><div className="eyebrow">КОНТЕКСТ ДИАЛОГА</div><h2 id="context-title">{context.hit.chat_name}</h2>
      <div className="context-nav"><button disabled={loadingContext} onClick={() => void openContext(context.hit, context.messages[0].message_id)}>← Более ранние</button><button disabled={loadingContext} onClick={() => void openContext(context.hit, context.messages[context.messages.length - 1].message_id)}>Более поздние →</button></div>
      <div className="context-messages">{context.messages.map(message => <MessageRow key={message.message_id} message={message} anchor={context.hit.message_id} query={submitted} />)}</div>
    </section></div>}
  </div>;
}
