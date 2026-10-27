export type Chat = {
  id: string; name: string; scope: string; messages: number; photos: number;
  date_from: number | null; date_to: number | null;
};
export type SearchModality = 'text' | 'images' | 'ocr';
export type SearchIntent = {
  schema_version?: 1; query: string; scope: { mode: 'all' | 'selected'; chat_ids?: string[] };
  author_ids: string[]; forwarded_from: string[]; date_from: string | null; date_to: string | null;
  content_type: string; exclude_deleted: boolean; exact_phrase: boolean; mode: string;
  modalities: SearchModality[]; strategy?: 'exact' | 'ann'; sort?: 'newest' | 'oldest';
  page_size?: number; chunk_size?: number;
};
export type RetrievalInfo = {
  requested_strategy?: 'exact' | 'ann'; effective_strategy?: 'none' | 'exact' | 'ann' | 'mixed';
  fallback_reasons?: string[];
};
export type SearchPage = RetrievalInfo & { results: Hit[]; has_more: boolean; effective_mode: string; warnings: string[];
  rerank_applied?: boolean; limit: number; job_id: string; browse_cursor?: string | null;
  next_offset?: number | null; cached_results?: number; spec?: SearchIntent;
  history_id?: string | null; snapshot_id?: string; completed_at?: number; favorite?: boolean };
export type SavedSearch = {
  id: string; snapshot_id: string; spec: SearchIntent; completed_at: number;
  favorite: boolean; recent: boolean; cache_available: boolean; stale_reason: string | null;
  cached_results: number; models: Record<string, unknown>; coverage: Record<string, unknown>;
};
export type RestoredSearch = { entry: SavedSearch; result: SearchPage | null };
export type SearchSnapshot = Omit<SavedSearch, 'id'> & { id: string | null };
export type TabView = { visible_count: number; scroll_y: number; display_settings_open: boolean };
export type SearchTab = {
  id: string; kind: 'search'; position: number; revision: number; draft: SearchIntent;
  view: TabView; snapshot: SearchSnapshot | null; job_id: string | null;
  interrupted: boolean; last_state: string | null; error: string | null;
};
export type TabList = { tabs: SearchTab[]; active_tab_id: string | null; capacity: number };
export type TabResult = { entry: SearchSnapshot | null; result: SearchPage | null };
export type SearchJobStatus = RetrievalInfo & {
  job_id: string; state: 'queued' | 'running' | 'cancelling' | 'cancelled' | 'completed' | 'failed';
  phase: string; completed: number; total: number | null; unit: string;
  elapsed_seconds: number; rate: number | null; eta_seconds: number | null;
  queue_position: number | null; error: string | null; warnings: string[];
};
export type ChatIndexStatus = {
  semantic: SemanticStatus; media: MediaStatus;
  chunking?: { profile: string; policy_id: string; max_messages: number; max_tokens: number; skipped_messages: number };
  excluded_author_ids?: string[]; excluded_messages?: number;
};
export type Message = {
  chat_id: string; message_id: number; timestamp: number; author: string;
  text: string; kind: string; action: string | null; reply_to: number | null;
  forwarded_from: string | null; matches_filters: boolean;
  media: { id: number; kind: string; status: string }[];
  edited_timestamp: number | null; remote_deleted?: number;
};
export type Hit = {
  chat_id: string; chat_name: string; message_id: number; timestamp: number;
  messages: Message[];
  matched_by?: ('words' | 'meaning' | 'image' | 'ocr_words' | 'ocr_meaning')[];
  result_type?: 'text' | 'image' | 'ocr' | 'message';
  media_id?: number;
  image_similarity?: number;
  ocr_text?: string | null;
  ocr_confidence?: number | null;
  ocr_match?: { kind: 'exact' | 'substring' | 'fuzzy'; edits: number };
  ocr_range?: { char_start: number; char_end: number };
  chunk_id?: string | null;
  matched_parts?: { message_id: number; char_start: number; char_end: number }[];
};

export type IndexWorkerHealth = {
  enabled: boolean; alive: boolean; stopped: boolean; phase: string;
  waiting_reason: 'database' | null; error: string | null;
  database_wait_seconds: number; retry_in_seconds: number; heartbeat_age_seconds: number;
};
export type IndexEstimate = {
  lower_seconds: number | null; upper_seconds: number | null;
  eta_samples?: number; measuring?: boolean; projected?: boolean; paused?: boolean; stale?: boolean;
  waiting_reason?: 'memory' | 'database' | null;
};

export type MediaStatus = {
  memory_wait?: { stages: ('clip' | 'ocr' | 'ocr_dense')[]; rss_bytes: number; limit_bytes: number; available_bytes: number | null } | null;
  ocr_estimate?: IndexEstimate; images_estimate?: IndexEstimate; ocr_dense_estimate?: IndexEstimate;
  preparation_estimate?: IndexEstimate;
  visual_profile?: 'clip' | 'siglip2';
  visual_profiles?: { profile: string; download_bytes: number }[];
  ocr_batch_size?: number; ocr_region_batch_size?: number;
  ocr_dense_paused?: boolean | number;
  ocr_dense_estimated_remaining_seconds?: number | null;
  preparation_estimated_remaining_seconds?: number | null;
  photo_attachments?: number; photo_messages?: number;
  queues?: Record<string, { pending: number; samples: number; units_per_minute: number | null;
    p50_seconds: number | null; p95_seconds: number | null; measuring: boolean }>;
  batch_size?: number;
  ocr_enabled: number; images_enabled: number; paused: number; running: boolean;
  ocr_paused?: boolean | number; ocr_engine?: 'tesseract' | 'paddle'; ocr_backend?: { device: string; provider: string };
  preparation_state: string; error: string | null; resource_error: string | null;
  total_photos: number; ocr_ready: number; ocr_failed: number; ocr_dense_ready: number;
  ocr_dense_failed?: number; ocr_nonempty_ready?: number; ocr_dense_available?: boolean;
  ocr_estimated_remaining_seconds?: number | null;
  images_ready: number; missing_refs: number; ocr_available: boolean; images_available: boolean;
  images_failed?: number; images_estimated_remaining_seconds?: number | null;
  ocr_runtime_installed: boolean; preparation_kind?: 'images' | 'ocr' | null; device: string; backend?: { device: string; provider: string };
  query_backend?: { device: string; provider: string };
};

export type SemanticStatus = {
  worker?: IndexWorkerHealth;
  estimate?: IndexEstimate | null;
  batch_size?: number;
  runtime_installed: boolean; dense_available: boolean; enabled: number; paused: number;
  backend?: { device: string; provider: string; warning?: string | null; query_execution?: { device: string; provider: string } };
  preparation_state: string; profile: string | null; error: string | null;
  index_error?: string | null;
  download_completed_bytes: number; download_total_bytes: number;
  total_segments: number; ready_segments: number; pending_segments: number;
  estimated_remaining_seconds?: number | null;
  works: { state: string; count: number; chunks_total: number; chunks_done: number }[];
  profiles: { profile: string; model_id: string; download_bytes: number; dimension: number; revision?: string }[];
};
export type Job = {
  id: string; chat_name: string; state: string; processed: number; added: number;
  updated: number; unchanged: number; conflicts: number; missing_media: number;
  invalid_media: number; error: string | null; warnings: string[];
  pending_conflicts: number;
  total_messages?: number | null;
  progress?: ImportTaskProgress | null;
};
export type ImportTaskProgress = {
  phase: string; completed: number; total: number | null; unit: 'bytes' | 'messages';
  elapsed_seconds: number; phase_elapsed_seconds: number; rate: number | null; eta_seconds: number | null;
  activity?: { phase: string; completed: number; total: number | null; unit: string } | null;
};
export type Preview = Omit<Job, 'pending_conflicts'> & {
  scope: string; root_relative_path: string; json_relative_path: string;
  preparing?: boolean; operation?: 'preview' | 'apply'; applied_import_id?: string | null;
};
export type Conflict = {
  message_id: number; reason: string; current: Message | null; current_version: string | null;
  current_metadata: Record<string, unknown>;
  incoming: { text: string; author: string; timestamp: number; edited_timestamp: number | null; metadata: Record<string, unknown> };
};
