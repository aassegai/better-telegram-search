import { api, ApiError } from './api';
import { t } from './i18n';
import type { SearchIntent, SearchTab, TabView } from './types';
import { clearTabJournal, saveTabJournal } from './SearchTabJournal';

type Pending = { draft?: SearchIntent; view?: TabView; snapshot_id?: string | null };

/** Serial, coalesced writes never apply an older server draft to a live editor. */
export default class SearchTabPersistence {
  private revision: number;
  private snapshot: string | null;
  private pending: Pending = {};
  private viewSnapshot: string | null = null;
  private running: Promise<void> | null = null;
  private timer: ReturnType<typeof setTimeout> | undefined;
  private closed = false;
  private serverDraft: SearchIntent;
  private inFlightDraft: SearchIntent | null = null;
  private journal: { draft: SearchIntent; view: TabView; snapshot: string | null } | null = null;

  constructor(private tab: SearchTab, private onError: (error: unknown) => void, private onMissing: () => void) {
    this.revision = tab.revision;
    this.snapshot = tab.snapshot?.snapshot_id ?? null;
    this.serverDraft = tab.draft;
  }

  adopt(tab: SearchTab) {
    if (tab.revision >= this.revision) {
      if (this.snapshot !== (tab.snapshot?.snapshot_id ?? null) && this.viewSnapshot !== (tab.snapshot?.snapshot_id ?? null)) delete this.pending.view;
      this.revision = tab.revision;
      this.snapshot = tab.snapshot?.snapshot_id ?? null;
      this.serverDraft = tab.draft;
    }
  }

  schedule(draft: SearchIntent, view: TabView, snapshot: string | null) {
    if (this.closed) return;
    this.pending = { ...this.pending, draft, view };
    this.viewSnapshot = snapshot;
    this.journal = { draft, view, snapshot };
    this.checkpoint();
    clearTimeout(this.timer);
    this.timer = setTimeout(() => { void this.flush().catch(this.onError); }, 250);
  }

  async flush(): Promise<void> {
    clearTimeout(this.timer);
    if (this.running) { await this.running; if (Object.keys(this.pending).length) return this.flush(); return; }
    if (this.closed || !Object.keys(this.pending).length) return;
    this.running = this.drain();
    try { await this.running; } finally { this.running = null; }
    if (Object.keys(this.pending).length) await this.flush();
  }

  resume() { this.closed = false; }

  private checkpoint() {
    if (this.journal) saveTabJournal(this.tab.id, this.journal,
      this.inFlightDraft ? [this.serverDraft, this.inFlightDraft] : [this.serverDraft]);
  }

  private async drain() {
    let conflicts = 0;
    while (!this.closed && Object.keys(this.pending).length) {
      if (await this.write() && ++conflicts >= 2) throw new Error(t('Вкладка изменена в другом окне. Обновите её состояние.'));
    }
  }

  private async write() {
    const patch = this.pending;
    const viewSnapshot = this.viewSnapshot;
    this.pending = {};
    if (viewSnapshot !== this.snapshot) delete patch.view;
    this.inFlightDraft = patch.draft ?? null;
    this.checkpoint();
    try {
      const value = await api<SearchTab>(`/api/search-tabs/${this.tab.id}`, {
        method: 'PATCH', body: JSON.stringify({ ...patch, revision: this.revision }),
      });
      this.adopt(value);
      this.inFlightDraft = null;
      if (Object.keys(this.pending).length) this.checkpoint();
      else { this.journal = null; clearTabJournal(this.tab.id); }
    } catch (error) {
      if (this.closed) return false;
      if (error instanceof ApiError && error.status === 404) {
        this.dispose(); clearTabJournal(this.tab.id); this.onMissing();
        return false;
      }
      if (!(error instanceof ApiError) || error.status !== 409) {
        this.pending = { ...patch, ...this.pending };
        throw error;
      }
      const current = await api<SearchTab>(`/api/search-tabs/${this.tab.id}`);
      this.adopt(current);
      // Publication may have reset the view while this write was in flight.
      // Retry only the editor's latest draft, never a stale view/snapshot binding.
      const draft = this.pending.draft ?? patch.draft;
      if (draft) this.pending.draft = draft;
      if (this.viewSnapshot !== this.snapshot) delete this.pending.view;
      this.inFlightDraft = null;
      this.checkpoint();
      return true;
    }
    return false;
  }

  async bind(snapshot: string, draft: SearchIntent, view: TabView): Promise<SearchTab> {
    await this.flush();
    let bound: SearchTab | undefined;
    const bind = async () => {
      if (this.closed) throw new Error(t('Вкладка закрыта.'));
      const current = await api<SearchTab>(`/api/search-tabs/${this.tab.id}`);
      this.adopt(current);
      bound = await api<SearchTab>(`/api/search-tabs/${this.tab.id}`, {
        method: 'PATCH', body: JSON.stringify({ revision: this.revision, snapshot_id: snapshot, draft, view }),
      });
      this.adopt(bound);
    };
    this.running = bind();
    try { await this.running; } finally { this.running = null; }
    await this.flush();
    return bound!;
  }

  dispose() { this.closed = true; clearTimeout(this.timer); this.pending = {}; }
}
