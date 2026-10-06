export type Chat = {
  id: string; name: string; scope: string; messages: number; photos: number;
  date_from: number | null; date_to: number | null;
};
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
