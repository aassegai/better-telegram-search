import type { SearchIntent, SearchTab, TabView } from './types';

const prefix = 'bts.pending-search-tab.';
type Journal = { draft: SearchIntent; view: TabView; snapshot: string | null; predecessors: string[] };

function draftKey(value: SearchIntent) {
  return JSON.stringify({ schema_version: 1, query: value.query,
    scope: { mode: value.scope.mode, chat_ids: [...(value.scope.chat_ids ?? [])].sort() },
    author_ids: [...value.author_ids].sort(), forwarded_from: [...value.forwarded_from].sort(),
    date_from: value.date_from ?? null, date_to: value.date_to ?? null,
    content_type: value.content_type, exclude_deleted: value.exclude_deleted,
    exact_phrase: value.exact_phrase, mode: value.mode, modalities: [...value.modalities].sort(),
    strategy: value.strategy ?? 'exact', sort: value.sort ?? 'newest',
    page_size: value.page_size ?? 20, chunk_size: value.chunk_size ?? 10 });
}

function validDraft(value: SearchIntent) {
  const strings = (values: unknown, limit: number) => Array.isArray(values) && values.length <= 500
    && values.every(item => typeof item === 'string' && item.length <= limit);
  return value && typeof value.query === 'string' && value.query.length <= 4096
    && value.scope && ['all', 'selected'].includes(value.scope.mode)
    && strings(value.scope.chat_ids ?? [], 256) && strings(value.author_ids, 256)
    && strings(value.forwarded_from, 4096)
    && [value.date_from, value.date_to].every(item => item === null || typeof item === 'string' && item.length <= 64)
    && ['all', 'text', 'photo', 'service'].includes(value.content_type)
    && typeof value.exclude_deleted === 'boolean' && typeof value.exact_phrase === 'boolean'
    && ['words', 'meaning', 'hybrid'].includes(value.mode)
    && Array.isArray(value.modalities) && value.modalities.length <= 3
    && value.modalities.every(item => ['text', 'images', 'ocr'].includes(item))
    && ['exact', 'ann'].includes(value.strategy ?? 'exact') && ['newest', 'oldest'].includes(value.sort ?? 'newest')
    && [value.page_size ?? 20, value.chunk_size ?? 10].every(item => Number.isInteger(item) && item >= 1 && item <= 100);
}

/** Only unsaved editor parameters, never messages/results; isolated per browser window. */
export function saveTabJournal(id: string, value: Omit<Journal, 'predecessors'>, previous: SearchIntent[]) {
  try {
    const raw = JSON.stringify({ ...value, predecessors: previous.map(draftKey) });
    if (raw.length <= 4 * 1024 ** 2) sessionStorage.setItem(prefix + id, raw);
    else clearTabJournal(id);
  } catch { /* SQLite remains authoritative when browser storage is unavailable. */ }
}

export function clearTabJournal(id: string) {
  try { sessionStorage.removeItem(prefix + id); } catch { /* Storage may be blocked. */ }
}

export function pruneTabJournals(ids: string[]) {
  try {
    for (const key of Object.keys(sessionStorage)) {
      if (key.startsWith(prefix) && !ids.includes(key.slice(prefix.length))) sessionStorage.removeItem(key);
    }
  } catch { /* Storage may be blocked. */ }
}

export function recoverTabJournal(tab: SearchTab): { draft: SearchIntent; view: TabView } | null {
  try {
    const raw = sessionStorage.getItem(prefix + tab.id);
    if (!raw || raw.length > 4 * 1024 ** 2) return null;
    const value: Journal = JSON.parse(raw);
    if (!validDraft(value.draft) || !Array.isArray(value.predecessors)
      || !value.predecessors.includes(draftKey(tab.draft))) return null;
    const view = value.view;
    const validView = view && Number.isInteger(view.visible_count) && view.visible_count >= 0 && view.visible_count <= 100
      && Number.isFinite(view.scroll_y) && view.scroll_y >= 0 && view.scroll_y <= 1e9 && typeof view.display_settings_open === 'boolean';
    return { draft: value.draft, view: validView && value.snapshot === (tab.snapshot?.snapshot_id ?? null) ? view : tab.view };
  } catch { return null; }
}
