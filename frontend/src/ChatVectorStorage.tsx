import { useEffect, useRef, useState } from 'react';
import { api } from './api';
import { t } from './i18n';
import Dialog from './Dialog';
import { useDialogOperation } from './useDialogOperation';
import type { Chat } from './types';

type Kind = 'text' | 'images' | 'ocr';
type Inventory = { indexes: Record<Kind, { vectors: number; bytes: number; shared: boolean; reclaimable_vectors: number; shared_vectors: number }> };
type Job = { chat_id: string | null; state: string; kind: Kind | null; error?: string | null; deleted_vectors?: number; freed_bytes?: number };
const kinds = [['text', 'Текстовые векторы'], ['images', 'Векторы изображений'], ['ocr', 'Векторы текста OCR']] as const;
const mib = (value: number) => t('{p0} МиБ', { p0: (value / 1024 ** 2).toFixed(1) });
const active = (job: Job) => job.state === 'waiting' || job.state === 'clearing';

export default function ChatVectorStorage({ chat, pending, onStart, onEnd }: {
  chat: Chat; pending: boolean; onStart: () => void; onEnd: () => void;
}) {
  const [inventory, setInventory] = useState<Inventory | null>(null);
  const [job, setJob] = useState<Job>({ state: 'idle', kind: null, chat_id: null });
  const [confirmation, setConfirmation] = useState<Kind | null>(null);
  const [initialized, setInitialized] = useState(false);
  const [statusLoading, setStatusLoading] = useState(true);
  const [inventoryLoading, setInventoryLoading] = useState(false);
  const inventoryRequest = useRef<AbortController | null>(null);
  const op = useDialogOperation();
  const working = active(job);
  const refresh = async (current: () => boolean) => {
    inventoryRequest.current?.abort();
    const request = new AbortController();
    inventoryRequest.current = request;
    setInventoryLoading(true);
    const timer = setTimeout(() => request.abort(), 10000);
    try {
      const value = await api<Inventory>(`/api/chats/${encodeURIComponent(chat.id)}/storage/vectors`, { signal: request.signal });
      if (current() && inventoryRequest.current === request) setInventory(value);
    } catch (error) {
      if (request.signal.aborted) throw new Error(t('Подсчёт размера индексов задерживается. Очистка доступна без этого подсчёта; сведения можно обновить позже.'));
      throw error;
    } finally {
      clearTimeout(timer);
      if (inventoryRequest.current === request) {
        inventoryRequest.current = null;
        setInventoryLoading(false);
      }
    }
  };
  useEffect(() => {
    if (!working) return;
    onStart();
    return onEnd;
  }, [working]);
  const reload = async (current: () => boolean) => {
    setStatusLoading(true);
    let value: Job;
    try {
      value = await api<Job>('/api/storage/vectors/clear', { signal: AbortSignal.timeout(10000) });
    } catch (error) {
      if (error instanceof Error && (error.name === 'TimeoutError' || error.name === 'AbortError')) throw new Error(t('Не удалось проверить состояние очистки. Обновите сведения, чтобы повторить запрос.'));
      throw error;
    } finally { setStatusLoading(false); }
    if (!current()) return;
    setJob(value);
    setInitialized(true);
    if (!active(value)) void refresh(current).catch(error => { if (current()) op.setError(error.message); });
  };
  useEffect(() => {
    let alive = true;
    const valid = op.guard();
    const current = () => alive && valid();
    void reload(current).catch(error => { if (current()) op.setError(error.message); });
    return () => { alive = false; inventoryRequest.current?.abort(); };
  }, []);
  useEffect(() => {
    if (!working) return;
    let alive = true;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try {
        const value = await api<Job>('/api/storage/vectors/clear');
        if (!alive) return;
        op.setError('');
        if (!active(value)) {
          setJob(value);
          const current = op.guard();
          void refresh(current).catch(error => { if (current()) op.setError(error instanceof Error ? error.message : t('Ошибка операции.')); });
          return;
        }
        setJob(value);
      } catch (error) { if (alive) op.setError(error instanceof Error ? error.message : t('Ошибка операции.')); }
      if (alive) timer = setTimeout(() => void poll(), 800);
    };
    timer = setTimeout(() => void poll(), 300);
    return () => { alive = false; clearTimeout(timer); };
  }, [working]);
  const clear = () => void op.run(async current => {
    if (!confirmation) return;
    inventoryRequest.current?.abort();
    const value = await api<Job>(`/api/chats/${encodeURIComponent(chat.id)}/storage/vectors/${confirmation}/clear`, { method: 'POST', body: JSON.stringify({ confirm: true }) });
    if (current()) {
      setJob(value); setConfirmation(null);
      if (!active(value)) void refresh(current).catch(error => { if (current()) op.setError(error.message); });
    }
  });
  const busy = op.busy || pending || working || !initialized;
  const ownJob = job.chat_id === chat.id;
  return <section className="storage-panel chat-vector-storage" aria-label={t('Индексы этого диалога')}>
    <h3>{t('Очистка индексов')}</h3>
    <p className="baseline-note">{t('Очистка затронет только этот диалог. Сообщения, фотографии, модели и распознанный текст OCR сохранятся. Выбранный индекс останется на паузе до ручного запуска.')}</p>
    {statusLoading && <p role="status">{t('Проверяем состояние очистки…')}</p>}
    {!statusLoading && inventoryLoading && !working && job.state !== 'done' && <p role="status">{t('Подсчитываем размер индексов. Очистка уже доступна.')}</p>}
    {initialized && !inventoryLoading && !inventory && !working && <p className="baseline-note">{t('Размер индексов пока неизвестен. Очистка доступна; общие с другими диалогами векторы сохранятся.')}</p>}
    {op.error && <p className="error" role="alert">{t(op.error)}</p>}
    <div className="vector-storage-list">{kinds.map(([kind, label]) => {
      const entry = inventory?.indexes[kind];
      const retryable = ownJob && job.state === 'failed' && job.kind === kind;
      return <section key={kind} className="vector-storage-card" aria-label={t(label)}>
        <div><strong>{t(label)}</strong>{entry && <small>{t('Векторов: {p0}', { p0: entry.vectors })}{entry.shared_vectors > 0 && t(' · общих с другими диалогами: {p0}', { p0: entry.shared_vectors })} · {entry.shared ? '≈ ' : ''}{mib(entry.bytes)}</small>}</div>
        <button className="danger" disabled={busy || (!retryable && !!entry && entry.reclaimable_vectors === 0 && entry.bytes === 0)} onClick={() => setConfirmation(kind)}>{t(retryable ? 'Повторить очистку' : 'Очистить')}</button>
      </section>;
    })}</div>
    <p className="baseline-note">{t('Общие с другими диалогами векторы сохраняются. Размер к освобождению приблизительный; после удаления файлы индекса уплотняются.')}</p>
    {working && <p role="status">{t(!ownJob ? 'Выполняется очистка другого диалога. Дождитесь завершения.' : job.state === 'waiting' ? 'Ожидаем завершения текущего батча…' : 'Удаляем векторы и освобождаем место…')}</p>}
    {ownJob && job.state === 'done' && <p role="status">{t('Индекс очищен. Удалено векторов: {p0}. Освобождено: {p1}.', { p0: job.deleted_vectors ?? 0, p1: mib(job.freed_bytes ?? 0) })}</p>}
    {ownJob && job.error && <p className="error" role="alert">{t(job.error)}</p>}
    <div className="job-actions"><button disabled={op.busy || pending || working || statusLoading || inventoryLoading} onClick={() => void op.run(reload)}>{t('Обновить сведения о месте')}</button></div>
    {confirmation && <Dialog titleId="clear-vector-title" onClose={() => { if (!op.busy) setConfirmation(null); }}>
      <h2 id="clear-vector-title">{t('Очистить векторный индекс?')}</h2>
      <p>{t(kinds.find(([kind]) => kind === confirmation)![1])}</p>
      <p>{t('Удалить векторы выбранного типа только для «{p0}»? Общие с другими диалогами векторы сохранятся. Для восстановления смыслового поиска запустите индексацию этого диалога.', { p0: chat.name })}</p>
      {op.error && <p className="error" role="alert">{t(op.error)}</p>}
      <div className="limit-presets"><button disabled={op.busy} onClick={() => setConfirmation(null)}>{t('Отмена')}</button><button className="danger" disabled={op.busy} onClick={clear}>{op.busy ? t('Запускаем…') : t('Очистить векторы')}</button></div>
    </Dialog>}
  </section>;
}
