export type Chat = {
  id: string; name: string; scope: string; messages: number; photos: number;
  date_from: number | null; date_to: number | null;
};
export type SearchModality = 'text' | 'images' | 'ocr';
export type Message = {
  chat_id: string; message_id: number; timestamp: number; author: string;
  text: string; kind: string; action: string | null; reply_to: number | null;
  forwarded_from: string | null; matches_filters: boolean;
  media: { id: number; kind: string; status: string }[];
  edited_timestamp: number | null;
};
export type Hit = {
  chat_id: string; chat_name: string; message_id: number; timestamp: number;
  messages: Message[];
  matched_by?: ('words' | 'meaning' | 'image' | 'ocr_words' | 'ocr_meaning')[];
  result_type?: 'text' | 'image' | 'ocr';
  media_id?: number;
  ocr_text?: string | null;
  ocr_confidence?: number | null;
  ocr_range?: { char_start: number; char_end: number };
  chunk_id?: string;
  matched_parts?: { message_id: number; char_start: number; char_end: number }[];
};

export type MediaStatus = {
  ocr_enabled: number; images_enabled: number; paused: number; running: boolean;
  preparation_state: string; error: string | null; resource_error: string | null;
  total_photos: number; ocr_ready: number; ocr_failed: number; ocr_dense_ready: number;
  images_ready: number; missing_refs: number; ocr_available: boolean; images_available: boolean;
  ocr_runtime_installed: boolean; device: string;
};

export type SemanticStatus = {
  runtime_installed: boolean; dense_available: boolean; enabled: number; paused: number;
  preparation_state: string; profile: string | null; error: string | null;
  download_completed_bytes: number; download_total_bytes: number;
  total_segments: number; ready_segments: number; pending_segments: number;
  estimated_remaining_seconds?: number | null;
  works: { state: string; count: number; chunks_total: number; chunks_done: number }[];
  profiles: { profile: string; model_id: string; download_bytes: number; dimension: number }[];
};
export type Job = {
  id: string; chat_name: string; state: string; processed: number; added: number;
  updated: number; unchanged: number; conflicts: number; missing_media: number;
  invalid_media: number; error: string | null; warnings: string[];
  pending_conflicts: number;
};
export type Preview = Omit<Job, 'pending_conflicts'> & {
  scope: string; root_relative_path: string; json_relative_path: string;
};
export type Conflict = {
  message_id: number; reason: string; current: Message | null; current_version: string | null;
  current_metadata: Record<string, unknown>;
  incoming: { text: string; author: string; timestamp: number; edited_timestamp: number | null; metadata: Record<string, unknown> };
};
