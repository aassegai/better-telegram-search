import { useEffect, useState } from 'react';
import { api } from './api';
import { t } from './i18n';
import { useDialogOperation } from './useDialogOperation';

type Source = { managed?: number; id: number; chat_name: string; path: string; available: boolean; media_refs: number; ready_refs: number };

export default function SourcePanel({ chatId }: { chatId: string }) {
  const [sources, setSources] = useState<Source[]>([]);
  const [paths, setPaths] = useState<Record<number, string>>({});
  const [notice, setNotice] = useState('');
  const op = useDialogOperation();
  const url = `/api/sources?chat_id=${encodeURIComponent(chatId)}`;
  useEffect(() => {
    let alive = true;
    api<Source[]>(url).then(value => { if (alive) setSources(value); })
      .catch(error => { if (alive) setNotice(error instanceof Error ? error.message : t('Ошибка соединения.')); });
    return () => { alive = false; };
  }, [url]);
  const check = (source: Source, relink: boolean) => void op.run(async current => {
    const result = await api<{ counts: Record<string, number> }>(`/api/sources/${source.id}/${relink ? 'relink' : 'check'}`, {
      method: 'POST', ...(relink ? { body: JSON.stringify({ path: paths[source.id] || source.path, expected_path: source.path }) } : {}),
    });
    const values = await api<Source[]>(url);
    if (current()) {
      setSources(values);
      setNotice(t('Проверено: {p0} доступно, {p1} отсутствует, {p2} изменилось. Изменённые и непроверенные файлы требуют повторного импорта.', { p0: result.counts.ready, p1: result.counts.missing, p2: result.counts.changed }));
    }
  });
  return <details className="chat-sources"><summary>{t('Источники')}</summary>
    {sources.map(source => <div className="source-row" key={source.id}><strong>{source.chat_name}</strong>
      <p>{source.available ? t('Папка доступна') : t('Папка отсутствует')}{t(' · вложений: ')}{source.ready_refs || 0} / {source.media_refs}</p>
      <label>{t('Папка источника')}<input aria-label={t('Папка источника {p0}', { p0: source.chat_name })} value={paths[source.id] ?? source.path} disabled={op.busy || !!source.managed} onChange={event => setPaths({ ...paths, [source.id]: event.target.value })} /></label>
      <div className="job-actions"><button disabled={op.busy} onClick={() => check(source, false)}>{t('Проверить файлы')}</button>
        <button disabled={op.busy || !!source.managed || !paths[source.id] || paths[source.id] === source.path} onClick={() => check(source, true)}>{t('Привязать новую папку')}</button></div>
    </div>)}
    {notice && <p role="status">{t(notice)}</p>}{op.error && <p className="error" role="alert">{t(op.error)}</p>}
  </details>;
}
