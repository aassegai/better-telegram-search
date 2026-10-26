import { useEffect, useState } from 'react';
import { api } from './api';
import { t } from './i18n';
import { useDialogOperation } from './useDialogOperation';
import SearchHistoryStorage from './SearchHistoryStorage';

type Sizes = { database_bytes: number; vectors_bytes: number; models_bytes: number; cache_bytes: number; reclaim_estimate_bytes: number };
const mib = (value: number) => t('{p0} МиБ', { p0: (value / 1024 ** 2).toFixed(1) });

export default function StoragePanel() {
  const [sizes, setSizes] = useState<Sizes | null>(null);
  const [initialized, setInitialized] = useState(false);
  const op = useDialogOperation();
  const refresh = async (current: () => boolean) => {
    const value = await api<Sizes>('/api/storage');
    if (current()) setSizes(value);
  };
  useEffect(() => {
    let alive = true;
    const valid = op.guard();
    const current = () => alive && valid();
    void refresh(current).catch(error => { if (current()) op.setError(error.message); })
      .finally(() => { if (current()) setInitialized(true); });
    return () => { alive = false; };
  }, []);
  const busy = op.busy || !initialized;
  return <section className="storage-panel" aria-label={t('Место на диске')}>
    <SearchHistoryStorage />
    <h3>{t('Место на диске')}</h3>
    {sizes && <dl className="diagnostics"><dt>{t('База')}</dt><dd>{mib(sizes.database_bytes)}</dd><dt>{t('Векторы')}</dt><dd>{mib(sizes.vectors_bytes)}</dd><dt>{t('Модели и словари')}</dt><dd>{mib(sizes.models_bytes)}</dd><dt>{t('Кэш')}</dt><dd>{mib(sizes.cache_bytes)}</dd><dt>{t('Можно освободить в SQLite')}</dt><dd>{mib(sizes.reclaim_estimate_bytes)}</dd></dl>}
    <p className="baseline-note">{t('Очистка текстовых, графических и OCR-векторов доступна в настройках каждого диалога.')}</p>
    <div className="job-actions"><button disabled={busy} onClick={() => void op.run(refresh)}>{t('Обновить сведения о месте')}</button>
      <button disabled={busy || !sizes} onClick={() => void op.run(async current => {
        await api<Sizes>('/api/storage/compact', { method: 'POST' }); if (current()) await refresh(current);
      })}>{t('Уплотнить базу и индексы')}</button></div>
    {op.error && <p className="error" role="alert">{t(op.error)}</p>}
  </section>;
}
