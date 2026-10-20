import { useEffect, useState } from 'react';
import { api } from './api';
import { t } from './i18n';

type Source = { name: string; messages?: number };
type SourcePage = { sources: Source[]; next_offset: number | null };
type Props = { chatIds: string[] | null; selected: string[]; onChange: (sources: string[]) => void;
  excludeDeleted: boolean; version: string; disabled: boolean };

export default function ForwardFilter({ chatIds, selected, onChange, excludeDeleted, version, disabled }: Props) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState('');
  const [retry, setRetry] = useState(0);
  const scope = JSON.stringify([chatIds, query, excludeDeleted, version]);
  const [pagination, setPagination] = useState({ scope, offset: 0 });
  const offset = pagination.scope === scope ? pagination.offset : 0;
  const [listing, setListing] = useState<SourcePage & { scope: string }>({ scope: '', sources: [], next_offset: null });
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  useEffect(() => { setPagination({ scope, offset: 0 }); }, [scope]);
  useEffect(() => {
    if (!open) return;
    let alive = true;
    const [chats, search, deleted] = JSON.parse(scope) as [string[] | null, string, boolean, string];
    setError('');
    if (chats?.length === 0) {
      setListing({ scope, sources: [], next_offset: null }); setLoading(false);
      return;
    }
    setLoading(true);
    const timer = window.setTimeout(() => {
      const params = new URLSearchParams({ q: search, offset: String(offset), exclude_deleted: String(deleted) });
      chats?.forEach(id => params.append('chat_id', id));
      api<SourcePage>(`/api/forwarded-sources?${params}`).then(value => {
        if (alive) setListing(previous => ({ ...value, scope,
          sources: [...new Map([...(offset > 0 && previous.scope === scope ? previous.sources : []), ...value.sources]
            .map(source => [source.name, source])).values()] }));
      }).catch(error => { if (alive) setError(error instanceof Error ? error.message : t('Ошибка соединения.')); })
        .finally(() => { if (alive) setLoading(false); });
    }, 250);
    return () => { alive = false; window.clearTimeout(timer); };
  }, [open, scope, offset, retry]);
  const sources = listing.scope === scope ? listing.sources : [];
  const known = new Set(sources.map(source => source.name));
  // Keep selections visible when another query or chat hides them from the page.
  const choices: Source[] = [...selected.filter(name => !known.has(name)).map(name => ({ name })), ...sources];
  const toggle = (name: string) => onChange(selected.includes(name) ? selected.filter(value => value !== name) : [...selected, name]);
  return <details className="forward-filter" onToggle={event => setOpen(event.currentTarget.open)}>
    <summary>{t('От кого переслано')} · {selected.length ? t('Выбрано источников: {p0}', { p0: selected.length }) : t('Все источники')}</summary>
    <fieldset disabled={disabled}>
      <p className="baseline-note">{t('Можно выбрать несколько авторов или каналов. Без выбора поиск идёт по всем сообщениям.')}</p>
      <label>{t('Найти автора пересылки')}<input type="search" maxLength={200} value={query} onChange={event => setQuery(event.target.value)} onKeyDown={event => { if (event.key === 'Enter') event.preventDefault(); }} /></label>
      <div className="forward-source-list" role="group" aria-label={t('Источники пересылок')}>
        {choices.map(source => <label key={source.name}><input type="checkbox" checked={selected.includes(source.name)}
          disabled={!selected.includes(source.name) && selected.length >= 500} onChange={() => toggle(source.name)} />
          <span>{source.name}{source.messages != null && <small>{t('Сообщений: {p0}', { p0: source.messages })}</small>}</span></label>)}
      </div>
      {loading && <p role="status">{t('Загружаем источники пересылок…')}</p>}
      {!loading && !error && !sources.length && <p>{t('Источники пересылок не найдены.')}</p>}
      {error && <p className="error" role="alert">{t(error)}</p>}
      <div className="job-actions">
        {listing.scope === scope && listing.next_offset != null && <button type="button" disabled={loading || Boolean(error)} onClick={() => setPagination({ scope, offset: listing.next_offset! })}>{t('Показать ещё источники')}</button>}
        {error && <button type="button" disabled={loading} onClick={() => setRetry(value => value + 1)}>{t('Повторить')}</button>}
        {selected.length > 0 && <button type="button" onClick={() => onChange([])}>{t('Снять выбор источников')}</button>}
      </div>
    </fieldset>
  </details>;
}
