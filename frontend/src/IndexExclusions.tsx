import { useEffect, useMemo, useState } from 'react';
import { api } from './api';
import { t } from './i18n';
import type { ChatIndexStatus } from './types';
import type { Mutation } from './IndexCard';
import { useDialogOperation } from './useDialogOperation';

type Author = { author_id: string; name: string; messages: number };
type Props = Mutation & { chatId: string; status: ChatIndexStatus | null; onSaved: (value: ChatIndexStatus) => void };

export default function IndexExclusions({ chatId, status, pending, onStart, onEnd, onSaved }: Props) {
  const [authors, setAuthors] = useState<Author[]>([]);
  const [selected, setSelected] = useState<string[]>([]);
  const [dirty, setDirty] = useState(false);
  const [query, setQuery] = useState('');
  const [limit, setLimit] = useState(100);
  const [reload, setReload] = useState(0);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState('');
  const op = useDialogOperation();
  const savedKey = JSON.stringify(status?.excluded_author_ids ?? []);
  useEffect(() => { if (!dirty) setSelected(JSON.parse(savedKey) as string[]); }, [savedKey, dirty]);
  useEffect(() => {
    let alive = true;
    setLoading(true); setLoadError('');
    api<Author[]>(`/api/authors?chat_id=${encodeURIComponent(chatId)}`).then(value => {
      if (alive) setAuthors(value);
    }).catch(error => {
      if (alive) setLoadError(error instanceof Error ? error.message : t('Ошибка соединения.'));
    }).finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; };
  }, [chatId, reload]);
  const choices = useMemo(() => {
    const known = new Set(authors.map(author => author.author_id));
    return [...authors, ...selected.filter(id => !known.has(id)).map(id => ({ author_id: id, name: '', messages: 0 }))];
  }, [authors, selected]);
  const filtered = choices.filter(author => `${author.name} ${author.author_id}`.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase()));
  const disabled = pending || op.busy || !status;
  const toggle = (id: string) => {
    setSelected(values => values.includes(id) ? values.filter(value => value !== id) : [...values, id]);
    setDirty(true);
  };
  const save = () => void op.run(async current => {
    onStart?.();
    try {
      const value = await api<ChatIndexStatus>(`/api/chats/${encodeURIComponent(chatId)}/index/settings`, {
        method: 'PATCH', body: JSON.stringify({ excluded_author_ids: selected }),
      });
      if (current()) { setDirty(false); onSaved(value); }
    } finally { onEnd?.(); }
  });
  return <section className="index-card index-exclusions" aria-label={t('Исключения авторов')}>
    <details>
    <summary>{t('Не индексировать авторов')} · {selected.length}</summary>
    <p className="baseline-note">{t('Сообщения, картинки и OCR выбранных авторов не участвуют в поиске и индексации этого диалога. Сообщения остаются в архиве и полном контексте.')}</p>
    <label>{t('Найти автора')}<input type="search" value={query} onChange={event => { setQuery(event.target.value); setLimit(100); }} /></label>
    {loading && <p role="status">{t('Загружаем авторов…')}</p>}
    {loadError && <p className="error" role="alert">{t(loadError)}</p>}
    <div className="index-author-list">
      {filtered.slice(0, limit).map(author => <label key={author.author_id}>
        <input type="checkbox" checked={selected.includes(author.author_id)} disabled={disabled || (!selected.includes(author.author_id) && selected.length >= 500)} onChange={() => toggle(author.author_id)} />
        <span><strong>{author.name || author.author_id}</strong><small>{author.author_id} · {t('Сообщений: {p0}', { p0: author.messages })}</small></span>
      </label>)}
      {!loading && !loadError && filtered.length === 0 && <p>{t('Авторы не найдены.')}</p>}
    </div>
    <div className="job-actions">
      {filtered.length > limit && <button type="button" onClick={() => setLimit(value => value + 100)}>{t('Показать ещё авторов')}</button>}
      <button type="button" disabled={loading || op.busy} onClick={() => setReload(value => value + 1)}>{t('Обновить список авторов')}</button>
      {selected.length > 0 && <button type="button" disabled={disabled} onClick={() => { setSelected([]); setDirty(true); }}>{t('Снять все исключения')}</button>}
    </div>
    <p className="baseline-note">{t('Выбрано авторов: {p0} / 500', { p0: selected.length })} · {t('Исключено сообщений: {p0}', { p0: status?.excluded_messages ?? 0 })}</p>
    {dirty && <p className="baseline-note">{t('После сохранения текстовые фрагменты за затронутые дни будут обновлены. Индексация на паузе останется на паузе. Готовый кэш картинок и OCR сохраняется.')}</p>}
    <button type="button" className="primary" disabled={disabled || !dirty} onClick={save}>{op.busy ? t('Сохраняем…') : t('Сохранить исключения')}</button>
    {op.error && <p className="error" role="alert">{t(op.error)}</p>}
    </details>
  </section>;
}
