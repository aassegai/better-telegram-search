import { useEffect, useState } from 'react';
import { api } from './api';
import { useDialogOperation } from './useDialogOperation';

type SearchSettings = { search_result_limit: number; display_chunk_size: number };

export default function SearchSettingsPanel() {
  const [settings, setSettings] = useState<SearchSettings | null>(null);
  const [saved, setSaved] = useState(false);
  const op = useDialogOperation();
  useEffect(() => {
    let alive = true;
    api<SearchSettings>('/api/settings').then(value => { if (alive) setSettings(value); })
      .catch(error => { if (alive) op.setError(error instanceof Error ? error.message : 'Не удалось прочитать настройки поиска.'); });
    return () => { alive = false; };
  }, []);
  const valid = settings && [settings.search_result_limit, settings.display_chunk_size].every(value => Number.isInteger(value) && value >= 1 && value <= 100);
  return <section className="search-settings-panel">
    <h3>Выдача поиска</h3>
    <p>Количество результатов задаёт максимум карточек. Размер фрагмента — максимум сообщений в одной карточке, включая совпадение и соседние сообщения. Диапазон: 1–100.</p>
    <form onSubmit={event => {
      event.preventDefault();
      if (!valid || !settings) return;
      void op.run(async current => {
        const value = await api<SearchSettings>('/api/settings', { method: 'PATCH', body: JSON.stringify({ search_result_limit: settings.search_result_limit, display_chunk_size: settings.display_chunk_size }) });
        if (current()) { setSettings(value); setSaved(true); }
      });
    }}>
      {settings && <div className="resource-fields">
        {([['search_result_limit', 'Количество результатов'], ['display_chunk_size', 'Сообщений в одном фрагменте']] as const).map(([key, label]) => <label key={key}>{label}<input type="number" aria-label={label} min="1" max="100" step="1" value={settings[key]} disabled={op.busy} onChange={event => { setSaved(false); setSettings({ ...settings, [key]: Number(event.target.value) }); }} /></label>)}
      </div>}
      <button type="submit" disabled={!valid || op.busy}>Сохранить настройки поиска</button>
    </form>
    {saved && <p role="status">Настройки поиска сохранены. Они применятся к следующему запросу; переиндексация не требуется.</p>}
    {op.error && <p className="error" role="alert">{op.error}</p>}
  </section>;
}
