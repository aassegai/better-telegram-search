import { forwardRef, useEffect, useImperativeHandle, useLayoutEffect, useRef, useState } from 'react';
import { api, ApiError } from './api';
import { t } from './i18n';
import Icon from './Icon';
import SearchPane from './SearchPane';
import { clearTabJournal, pruneTabJournals } from './SearchTabJournal';
import type { SearchPaneHandle } from './SearchPane';
import type { Chat, MediaStatus, RestoredSearch, SearchTab, SemanticStatus, TabList } from './types';

export type SearchWorkspaceHandle = {
  setScope: (scope: string[] | null) => void;
  toggleChat: (id: string) => void;
  removedChat: (id: string) => void;
  restore: (value: RestoredSearch, repeat: boolean, newTab?: boolean) => void;
};
type Props = {
  connected: boolean; chats: Chat[]; chatsLoaded: boolean; authorsVersion: string;
  semantic: SemanticStatus | null; media: MediaStatus | null;
  onScope: (scope: string[] | null) => void; onReady: (ready: boolean) => void;
};

const SearchWorkspace = forwardRef<SearchWorkspaceHandle, Props>(function SearchWorkspace(props, ref) {
  const [tabs, setTabs] = useState<SearchTab[]>([]);
  const [active, setActive] = useState<string | null>(null);
  const [activation, setActivation] = useState(0);
  const [capacity, setCapacity] = useState(8);
  const [loaded, setLoaded] = useState(false);
  const [managing, setManaging] = useState(false);
  const [transitioning, setTransitioning] = useState(false);
  const [error, setError] = useState('');
  const [labels, setLabels] = useState<Record<string, { query: string; mode: string; busy: boolean }>>({});
  const panes = useRef(new Map<string, SearchPaneHandle>());
  const alive = useRef(true);
  const management = useRef(false);
  const reconcileNeeded = useRef(false);
  const currentActive = useRef(active);
  const focusTarget = useRef<string | null>(null);
  currentActive.current = active;
  const report = (error: unknown) => { if (alive.current) setError(error instanceof Error ? error.message : t('Ошибка соединения.')); };
  const select = (id: string) => { setActive(id); setActivation(value => value + 1); };

  useLayoutEffect(() => {
    if (!managing && focusTarget.current === active) {
      document.getElementById(`search-tab-${active}`)?.focus();
      focusTarget.current = null;
    }
  }, [active, managing]);

  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  useEffect(() => {
    if (!props.connected) return;
    let current = true;
    const load = async () => {
      const value = await api<TabList>('/api/search-tabs');
      if (!current) return;
      let values = value.tabs;
      if (!values.length) {
        const created = await api<SearchTab>('/api/search-tabs', { method: 'POST', body: '{}' });
        if (!current) return;
        values = [created];
      }
      pruneTabJournals(values.map(tab => tab.id));
      setCapacity(value.capacity); setTabs(values);
      select(values.some(tab => tab.id === value.active_tab_id) ? value.active_tab_id! : values[0].id);
      setLoaded(true); props.onReady(true);
    };
    void load().catch(report);
    return () => { current = false; };
  }, [props.connected]);

  const manage = async (operation: () => Promise<void>, transition = false) => {
    if (management.current) return;
    management.current = true; setManaging(true); setTransitioning(transition); setError('');
    try { await operation(); } catch (error) { report(error); }
    finally {
      management.current = false;
      if (alive.current) {
        setManaging(false); setTransitioning(false);
        if (reconcileNeeded.current) requestReconcile();
      }
    }
  };
  async function reconcile() {
    reconcileNeeded.current = false;
    const value = await api<TabList>('/api/search-tabs');
    let values = value.tabs;
    if (!values.length) values = [await api<SearchTab>('/api/search-tabs', { method: 'POST', body: '{}' })];
    if (!alive.current) return;
    setTabs(values); setCapacity(value.capacity); setLoaded(true); props.onReady(true);
    pruneTabJournals(values.map(tab => tab.id));
    if (!values.some(tab => tab.id === currentActive.current)) {
      focusTarget.current = value.active_tab_id ?? values[0].id;
      select(focusTarget.current);
    }
  }
  function requestReconcile() {
    reconcileNeeded.current = true;
    if (!management.current) void manage(reconcile, true);
  }
  const activate = (id: string) => {
    if (id === active || management.current) return;
    void manage(async () => {
      await panes.current.get(active ?? '')?.flush();
      try { await api(`/api/search-tabs/${id}/activate`, { method: 'POST' }); }
      catch (error) {
        if (error instanceof ApiError && error.status === 404) { reconcileNeeded.current = true; return; }
        throw error;
      }
      if (alive.current) { focusTarget.current = id; select(id); }
    }, true);
  };
  const create = (saved?: RestoredSearch) => void manage(async () => {
    await panes.current.get(currentActive.current ?? '')?.flush();
    const value = await api<SearchTab>('/api/search-tabs', {
      method: 'POST', body: JSON.stringify(saved ? { draft: saved.entry.spec,
        snapshot_id: saved.entry.snapshot_id,
        view: { visible_count: saved.result?.results.length ?? 0, scroll_y: 0, display_settings_open: false } } : {}),
    });
    if (alive.current) { setTabs(values => [...values, value]); focusTarget.current = value.id; select(value.id); }
  }, true);
  const close = (id: string) => void manage(async () => {
    try { await api(`/api/search-tabs/${id}`, { method: 'DELETE' }); }
    catch (error) { if (!(error instanceof ApiError) || error.status !== 404) throw error; }
    clearTabJournal(id);
    const value = await api<TabList>('/api/search-tabs');
    let values = value.tabs;
    if (!values.length) values = [await api<SearchTab>('/api/search-tabs', { method: 'POST', body: '{}' })];
    if (!alive.current) return;
    setTabs(values);
    const next = values.some(tab => tab.id === value.active_tab_id) ? value.active_tab_id! : values[0].id;
    focusTarget.current = next;
    if (next !== currentActive.current) select(next);
    setLabels(labels => { const next = { ...labels }; delete next[id]; return next; });
  }, id === currentActive.current);
  useImperativeHandle(ref, () => ({
    setScope: scope => panes.current.get(currentActive.current ?? '')?.setScope(scope),
    toggleChat: id => panes.current.get(currentActive.current ?? '')?.toggleChat(id),
    removedChat: id => panes.current.forEach(pane => pane.removedChat(id)),
    restore: (saved, repeat, newTab = false) => {
      if (newTab) create(saved);
      else panes.current.get(currentActive.current ?? '')?.restore(saved, repeat);
    },
  }));
  const title = (tab: SearchTab) => {
    const value = labels[tab.id] ?? tab.draft;
    return value.query.trim() || t('Новый поиск');
  };
  const modeLabel = (tab: SearchTab) => t(({ words: 'По словам', meaning: 'По смыслу', hybrid: 'Слова и смысл' } as Record<string, string>)[labels[tab.id]?.mode ?? tab.draft.mode] ?? 'Поиск');
  return <>
    <div className="search-tabs-toolbar">
      <div role="tablist" aria-label={t('Вкладки поиска')} className="search-tabs">
        {tabs.map((tab, index) => <div className={`search-tab ${active === tab.id ? 'selected' : ''}`} key={tab.id}>
          <button type="button" role="tab" id={`search-tab-${tab.id}`} aria-selected={active === tab.id}
            aria-controls={`search-pane-${tab.id}`} tabIndex={active === tab.id ? 0 : -1}
            aria-disabled={managing} title={title(tab)} onClick={() => activate(tab.id)} onKeyDown={event => {
              const next = event.key === 'ArrowRight' ? (index + 1) % tabs.length : event.key === 'ArrowLeft' ? (index - 1 + tabs.length) % tabs.length : event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1 : null;
              if (next !== null) { event.preventDefault(); activate(tabs[next].id); }
              if (event.key === 'Delete') { event.preventDefault(); close(tab.id); }
            }}><span>{title(tab)}<small>{modeLabel(tab)}</small></span>{labels[tab.id]?.busy && <span className="tab-busy" aria-label={t('Ищем…')}>●</span>}</button>
          <button type="button" className="close-search-tab" disabled={managing} aria-label={t('Закрыть вкладку {p0}', { p0: title(tab) })}
            onClick={() => close(tab.id)}><Icon name="close" size={14} /></button>
        </div>)}
      </div>
      <button type="button" className="new-search-tab" disabled={!loaded || managing || tabs.length >= capacity}
        aria-label={t('Новая вкладка поиска')} title={t('Новая вкладка поиска')} onClick={() => create()}><Icon name="plus" size={18} /></button>
    </div>
    {error && <div className="error tabs-error" role="alert">{t(error)} <button type="button" disabled={managing} onClick={requestReconcile}>{t('Обновить вкладки')}</button></div>}
    {!loaded && <div className="content" role="status">{t('Загрузка вкладок…')}</div>}
    {tabs.map(tab => <SearchPane key={tab.id} tab={tab} active={active === tab.id} activation={activation} transitioning={transitioning}
      ref={pane => { if (pane) panes.current.set(tab.id, pane); else panes.current.delete(tab.id); }}
      chats={props.chats} chatsLoaded={props.chatsLoaded} connected={props.connected} authorsVersion={props.authorsVersion}
      semantic={props.semantic} media={props.media} onScope={props.onScope} onMissing={requestReconcile}
      onLabel={(query, mode, busy) => setLabels(previous => previous[tab.id]?.query === query && previous[tab.id]?.mode === mode && previous[tab.id]?.busy === busy ? previous : { ...previous, [tab.id]: { query, mode, busy } })} />)}
  </>;
});
export default SearchWorkspace;
