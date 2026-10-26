import { api } from './api';
import { t } from './i18n';
import { useDialogOperation } from './useDialogOperation';
import { useState } from 'react';

const collections = [
  ['cache', 'Очистить сохранённую выдачу', 'Удалить сохранённые результаты? Запросы и избранное сохранятся.'],
  ['history', 'Очистить историю', 'Удалить обычную историю? Избранные поиски сохранятся.'],
  ['favorites', 'Очистить избранное', 'Убрать все поиски из избранного? Последние 10 поисков останутся в истории.'],
] as const;

export default function SearchHistoryStorage() {
  const op = useDialogOperation();
  const [done, setDone] = useState(false);
  return <section className="history-storage">
    <h3>{t('История и сохранённая выдача')}</h3>
    <div className="dialog-actions">{collections.map(([kind, label, prompt]) => <button key={kind}
      type="button" className="secondary" disabled={op.busy} onClick={() => {
        if (!window.confirm(t(prompt))) return;
        setDone(false);
        void op.run(async current => {
          await api(`/api/search-history/collections/${kind}`, { method: 'DELETE' });
          if (current()) { setDone(true); window.dispatchEvent(new Event('bts-search-history-changed')); }
        });
      }}>{t(label)}</button>)}</div>
    {done && <p role="status">{t('Сохранённые поиски обновлены.')}</p>}
    {op.error && <p className="error" role="alert">{t(op.error)}</p>}
  </section>;
}
