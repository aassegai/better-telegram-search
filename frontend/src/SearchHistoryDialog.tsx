import { useEffect, useState } from 'react';
import { api } from './api';
import { t, uiLocale } from './i18n';
import SettingsDialog from './SettingsDialog';
import FavoriteButton from './FavoriteButton';
import { useDialogOperation } from './useDialogOperation';
import type { RestoredSearch, SavedSearch } from './types';

type Page = { entries: SavedSearch[]; next_offset: number | null };

export default function SearchHistoryDialog({ onClose, onRestore }: {
  onClose: () => void; onRestore: (value: RestoredSearch, repeat: boolean, newTab?: boolean) => void;
}) {
  const [page, setPage] = useState<Page | null>(null);
  const op = useDialogOperation();
  useEffect(() => {
    let alive = true;
    const controller = new AbortController();
    api<Page>('/api/search-history', { signal: controller.signal }).then(value => { if (alive) setPage(value); })
      .catch(error => { if (alive) op.setError(error.message); });
    return () => { alive = false; controller.abort(); };
  }, []);
  const restore = (id: string, repeat: boolean, newTab = false) => void op.run(async current => {
    const value = await api<RestoredSearch>(`/api/search-history/${encodeURIComponent(id)}`);
    if (current()) onRestore(value, repeat, newTab);
  });
  const favorite = (entry: SavedSearch) => void op.run(async current => {
    await api(`/api/search-history/${encodeURIComponent(entry.id)}/favorite`, {
      method: 'POST', body: JSON.stringify({ favorite: !entry.favorite }),
    });
    if (current()) setPage(value => value && ({ ...value,
      next_offset: value.next_offset == null ? null : value.next_offset - Number(entry.favorite && !entry.recent),
      entries: value.entries.map(item => item.id === entry.id ? { ...item, favorite: !item.favorite } : item)
        .filter(item => item.recent || item.favorite),
    }));
  });
  return <SettingsDialog titleId="history-title" onClose={onClose}>
    <h2 id="history-title">{t('История поиска')}</h2>
    <p>{t('Последние 10 завершённых поисков и избранное. Открытие восстанавливает сохранённую выдачу; повтор запускает новый поиск.')}</p>
    {!page && <p role="status">{t('Загружаем…')}</p>}
    {page && !page.entries.length && <p>{t('История пока пуста.')}</p>}
    <div className="search-history-list">{page?.entries.map(entry => <article className="saved-search" key={entry.id}>
      <div className="saved-search-heading"><strong>{entry.spec.query || t('Сообщения по фильтрам')}</strong>
        <FavoriteButton selected={entry.favorite} disabled={op.busy} onClick={() => favorite(entry)} /></div>
      <time dateTime={new Date(entry.completed_at * 1000).toISOString()}>{new Date(entry.completed_at * 1000).toLocaleString(uiLocale())}</time>
      <small>{entry.spec.scope.mode === 'all' ? t('Все диалоги') : t('Выбрано диалогов: {p0}', { p0: entry.spec.scope.chat_ids?.length ?? 0 })}
        {' · '}{entry.spec.query ? t(entry.spec.mode === 'words' ? 'По словам' : entry.spec.mode === 'meaning' ? 'По смыслу' : 'Слова и смысл') : t('Просмотр сообщений')}</small>
      {!entry.cache_available && <p className="warning-note">{t('Сохранённая выдача недоступна. Параметры и избранное сохранены; повторите поиск.')}</p>}
      <div className="dialog-actions">
        <button type="button" className="secondary" disabled={op.busy} onClick={() => restore(entry.id, false)}>{t('Открыть поиск')}</button>
        <button type="button" className="secondary" disabled={op.busy} onClick={() => restore(entry.id, false, true)}>{t('Открыть в новой вкладке')}</button>
        <button type="button" className="text-button" disabled={op.busy} onClick={() => restore(entry.id, true)}>{t('Повторить поиск')}</button>
      </div>
    </article>)}</div>
    {page?.next_offset != null && <button type="button" disabled={op.busy} onClick={() => void op.run(async current => {
      const next = await api<Page>(`/api/search-history?offset=${page.next_offset}`);
      if (current()) setPage(value => ({ entries: [...(value?.entries ?? []), ...next.entries], next_offset: next.next_offset }));
    })}>{t('Показать ещё')}</button>}
    {op.error && <p className="error" role="alert">{t(op.error)}</p>}
  </SettingsDialog>;
}
