import { t, uiLocale } from './i18n';
import { useEffect, useState } from 'react';
import { api } from './api';
import { useDialogOperation } from './useDialogOperation';
import type { Conflict, Job } from './types';

const date = (timestamp: number | null) => timestamp === null ? t('не указана') : new Date(timestamp * 1000).toLocaleString(uiLocale());
const labels: Record<string, string> = {
  type: 'Тип сообщения', date: 'Исходная дата', date_unixtime: 'Исходная дата (Unix)',
  from_id: 'ID автора', actor_id: 'ID участника', text: 'Текст с форматированием',
  text_entities: 'Форматирование и ссылки', reply_to_message_id: 'Ответ на сообщение',
  forwarded_from: 'Источник пересылки', forwarded_from_id: 'ID источника пересылки',
  saved_from: 'Сохранено из', via_bot: 'Бот', grouped_id: 'Группа медиа', album_id: 'Альбом',
  action: 'Служебное событие', actor: 'Участник', members: 'Участники', media_type: 'Тип медиа',
  mime_type: 'Формат файла', sticker_emoji: 'Эмодзи стикера', duration_seconds: 'Длительность',
  media: 'Вложения: тип и SHA-256 содержимого (или путь отсутствующего файла)',
};
const value = (item: unknown) => item === undefined ? t('Не указано') : typeof item === 'string' ? item : JSON.stringify(item, null, 2);

function Details({ metadata, other }: { metadata: Record<string, unknown>; other: Record<string, unknown> }) {
  const changed = [...new Set([...Object.keys(metadata), ...Object.keys(other)])]
    .filter(key => JSON.stringify(metadata[key]) !== JSON.stringify(other[key]));
  return <dl className="metadata-diff">{changed.map(key => <div key={key}><dt>{t(labels[key] || key)}</dt><dd>{value(metadata[key])}</dd></div>)}</dl>;
}

export default function ConflictDialog({ job, onClose, onChanged }: {
  job: Job; onClose: () => void; onChanged: () => Promise<void>;
}) {
  const [items, setItems] = useState<Conflict[]>([]);
  const [pending, setPending] = useState(job.pending_conflicts);
  const [more, setMore] = useState(false);
  const { busy, error, setError, guard, run } = useDialogOperation();
  const load = async () => {
    const current = guard();
    const result = await api<{ results: Conflict[]; pending: number; has_more: boolean }>(`/api/imports/${job.id}/conflicts`);
    if (current()) { setItems(result.results); setPending(result.pending); setMore(result.has_more); }
  };
  useEffect(() => {
    let alive = true;
    api<{ results: Conflict[]; pending: number; has_more: boolean }>(`/api/imports/${job.id}/conflicts`).then(result => {
      if (alive) { setItems(result.results); setPending(result.pending); setMore(result.has_more); }
    }).catch(error => { if (alive) setError(error instanceof Error ? error.message : t('Ошибка списка.')); });
    return () => { alive = false; };
  }, [job.id]);

  async function resolve(item: Conflict, choice: string) {
    await run(async current => {
      await api(`/api/imports/${job.id}/conflicts/${item.message_id}`, { method: 'POST', body: JSON.stringify({ choice, expected_version: item.current_version }) });
      if (current()) { await load(); if (current()) await onChanged(); }
    });
  }

  return <section className="modal conflict-modal" role="dialog" aria-modal="true" aria-labelledby="conflict-title">
    <button className="close" aria-label={t("Закрыть конфликты")} onClick={onClose}>×</button>
    <div className="eyebrow">{t("ВЫБОР РЕДАКЦИЙ")}</div><h2 id="conflict-title">{t("Конфликты импорта")}</h2>
    <p>{t("Неразрешённых: ")}{pending}{t(". В базе сохраняется текущая версия, пока вы не выберете другую.")}</p>
    {error && <div className="error" role="alert">{t(error)}<button disabled={busy} onClick={() => void run(async () => { await load(); })}>{t("Обновить список")}</button></div>}
    {items.map(item => <article className="conflict-item" key={item.message_id}>
      <h3>{t("Сообщение #")}{item.message_id} · {item.reason === 'remote_deleted' ? t('Сообщение удалено в Telegram') : item.reason === 'older_revision' ? t('В экспорте более старая редакция') : t('Нет надёжной даты редакции')}</h3>
      <div className="conflict-versions"><div><strong>{t("Текущая версия")}</strong><small>{item.current?.author} · {date(item.current?.timestamp ?? null)}</small><small>{t("Редакция: ")}{date(item.current?.edited_timestamp ?? null)}</small><p>{item.current?.text || t('Без текста')}</p><Details metadata={item.current_metadata} other={item.incoming.metadata} /></div>
        <div><strong>{t("Версия из экспорта")}</strong><small>{item.incoming.author} · {date(item.incoming.timestamp)}</small><small>{t("Редакция: ")}{date(item.incoming.edited_timestamp)}</small><p>{item.incoming.text || t('Без текста')}</p><Details metadata={item.incoming.metadata} other={item.current_metadata} /></div></div>
      <div className="dialog-actions"><button disabled={busy || !item.current_version} onClick={() => void resolve(item, 'keep_current')}>{t("Оставить текущую")}</button><button className="primary" disabled={busy || !item.current_version || item.reason === 'remote_deleted'} onClick={() => void resolve(item, 'use_imported')}>{t("Использовать версию из экспорта")}</button></div>
    </article>)}
    {!items.length && !pending && <p>{t("Все конфликты разрешены.")}</p>}
    {more && <p>{t("Показаны первые 30 конфликтов. После выбора версий появятся следующие.")}</p>}
  </section>;
}
