import { t, uiLocale, useLanguage } from './i18n';
import { useEffect, useRef, useState } from 'react';
import { api, initializeSession } from './api';
import ImportDialog from './ImportDialog';
import ImportProgress from './ImportProgress';
import ConflictDialog from './ConflictDialog';
import ChatIndexDialog from './ChatIndexDialog';
import TelegramPanel from './TelegramPanel';
import type { Update } from './UpdatePanel';
import SearchHistoryDialog from './SearchHistoryDialog';
import SearchWorkspace from './SearchWorkspace';
import type { SearchWorkspaceHandle } from './SearchWorkspace';
import SettingsCenter, { initialSettingsSection } from './SettingsCenter';
import type { SettingsSection } from './SettingsCenter';
import Icon from './Icon';
import LanguageSwitch from './LanguageSwitch';
import SettingsDialog from './SettingsDialog';
import { useTheme } from './theme';
import './theme.css';
import type { Chat, Job, MediaStatus, Preview, SemanticStatus } from './types';

const stateNames: Record<string, string> = {
  queued: 'В очереди', running: 'Импортируется', completed: 'Поиск доступен',
  paused: 'Приостановлено', interrupted: 'Прервано', cancelled: 'Отменено', failed: 'Ошибка',
};
export default function App() {
  const [language, changeLanguage] = useLanguage();
  const [theme, toggleTheme] = useTheme();
  const [chats, setChats] = useState<Chat[]>([]);
  const [chatsLoaded, setChatsLoaded] = useState(false);
  const [selected, setSelected] = useState<string[] | null>(null);
  const [tabsReady, setTabsReady] = useState(false);
  const workspace = useRef<SearchWorkspaceHandle>(null);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [previews, setPreviews] = useState<Preview[]>([]);
  const [activePreview, setActivePreview] = useState<Preview | null>(null);
  const [activeImport, setActiveImport] = useState<Job | null>(null);
  const [semantic, setSemantic] = useState<SemanticStatus | null>(null);
  const [media, setMedia] = useState<MediaStatus | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [connected, setConnected] = useState(false);
  const [error, setError] = useState('');
  const [modal, setModal] = useState<'import' | 'settings' | 'sources' | null>(null);
  const [indexChat, setIndexChat] = useState<Chat | null>(null);
  const [conflictJob, setConflictJob] = useState<Job | null>(null);
  const [settingsSection, setSettingsSection] = useState<SettingsSection>(initialSettingsSection);
  const [performanceScope, setPerformanceScope] = useState<string | null>(null);
  const authorsVersion = jobs.map(job => `${job.id}:${job.processed}:${job.state}:${job.pending_conflicts}`).join('|');
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
    const escape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') { setModal(null); setHistoryOpen(false); setIndexChat(null); setConflictJob(null); }
    };
    window.addEventListener('keydown', escape);
    return () => window.removeEventListener('keydown', escape);
  }, []);

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
      workspace.current?.removedChat(chat.id);
      await refresh();
    } catch (error) { reportError(error); }
    finally { setDeleting(false); }
  }

  function settings(section?: SettingsSection, scope?: string | null) {
    if (section) { setSettingsSection(section); try { localStorage.setItem('bts.settings-section', section); } catch { /* The current selection remains in memory. */ } }
    if (scope !== undefined) setPerformanceScope(scope);
    setModal('settings');
  }

  return <div className="app">
    <aside className="sidebar">
      <a href="/" className="brand"><span className="brand-icon"><Icon name="archive" size={26} /></span>
        <span>{t("Архив")}<small>TELEGRAM SEARCH</small></span></a>
      <div className="sidebar-content">
      <div className="sidebar-title"><span>{t("Диалоги")}</span><button className="text-button" disabled={!tabsReady} onClick={() => workspace.current?.setScope(null)}>{t("Все")}</button></div>
      <div className="chat-list">
        {chats.map(chat => <div className="chat-item" key={chat.id}>
          <label><input type="checkbox" checked={selected === null || selected.includes(chat.id)} disabled={!tabsReady} onChange={() => workspace.current?.toggleChat(chat.id)} />
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
        <button type="button" className="text-button" disabled={!tabsReady} onClick={() => setHistoryOpen(true)}>{t('История поиска')}</button>
        <button className="theme-toggle mobile-settings-button" type="button" aria-label={t('Настройки')} title={t('Настройки')} onClick={() => void settings()}><Icon name="settings" /></button>
        <button className="theme-toggle" type="button" aria-label={t('Тёмная тема')} aria-pressed={theme === 'dark'} title={t('Тёмная тема')} onClick={toggleTheme}><Icon name={theme === 'dark' ? 'sun' : 'moon'} /></button>
        {!modal && !indexChat && !historyOpen && <LanguageSwitch value={language} onChange={changeLanguage} />}
      </div></header>
      {error && <div className="error tabs-error" role="alert"><span>{t(error)}</span><button aria-label={t('Закрыть ошибку')} onClick={() => setError('')}><Icon name="close" /></button></div>}
      <SearchWorkspace ref={workspace} connected={connected} chats={chats} chatsLoaded={chatsLoaded}
        authorsVersion={authorsVersion} semantic={semantic} media={media} onScope={setSelected} onReady={setTabsReady} />
      <footer className="main-footer">{t('Сообщения хранятся и обрабатываются на этом компьютере.')}</footer>
    </main>
    {modal === 'import' && <ImportDialog chats={chats} initialPreview={activePreview} initialImport={activeImport} onClose={() => setModal(null)} onApplied={() => { void refresh().catch(reportError); }} />}
    {historyOpen && <SearchHistoryDialog onClose={() => setHistoryOpen(false)} onRestore={(value, repeat, newTab) => { setHistoryOpen(false); workspace.current?.restore(value, repeat, newTab); }} />}
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

  </div>;
}
