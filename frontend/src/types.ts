export type Chat = {
  id: string; name: string; scope: string; messages: number; photos: number;
  date_from: number | null; date_to: number | null;
};
export type Message = {
  chat_id: string; message_id: number; timestamp: number; author: string;
  text: string; kind: string; action: string | null; reply_to: number | null;
  forwarded_from: string | null; matches_filters: boolean;
  media: { id: number; kind: string; status: string }[];
};
export type Hit = {
  chat_id: string; chat_name: string; message_id: number; timestamp: number;
  messages: Message[];
};
export type Job = {
  id: string; chat_name: string; state: string; processed: number; added: number;
  updated: number; unchanged: number; conflicts: number; missing_media: number;
  invalid_media: number; error: string | null; warnings: string[];
};
