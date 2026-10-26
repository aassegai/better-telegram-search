import { Fragment, useEffect, useState } from 'react';
import { api } from './api';
import { t } from './i18n';
import ImportProgress from './ImportProgress';
import type { Job } from './types';
import { useDialogOperation } from './useDialogOperation';

export default function ImportJobPanel({ id, initialJob, onChanged }: {
  id: string; initialJob?: Job; onChanged: () => void;
}) {
  const [job, setJob] = useState<Job | null>(initialJob ?? null);
  const op = useDialogOperation();
  useEffect(() => {
    if (op.busy) return;
    let alive = true, pending = false;
    const controller = new AbortController();
    const current = op.guard();
    const read = async () => {
      if (pending) return; pending = true;
      try {
        const value = await api<Job>(`/api/imports/${encodeURIComponent(id)}`, { signal: controller.signal });
        if (alive && current()) setJob(value);
        if (!['running', 'queued'].includes(value.state)) window.clearInterval(timer);
      } catch (error) { if (alive && current()) op.setError(error instanceof Error ? error.message : t('Ошибка соединения.')); }
      finally { pending = false; }
    };
    const timer = window.setInterval(() => void read(), 500); void read();
    return () => { alive = false; controller.abort(); window.clearInterval(timer); };
  }, [id, op.busy]);
  const control = (action: string) => void op.run(async current => {
    const value = await api<Job>(`/api/imports/${encodeURIComponent(id)}/${action}`, { method: 'POST' });
    if (current()) { setJob(value); onChanged(); }
  });
  return <section className="import-job-panel">
    {job ? <>
      <h3>{job.chat_name}</h3>
      <ImportProgress progress={job.progress} state={job.state} />
      {job.state === 'completed' && <p role="status">{t('Импорт завершён. Сообщения доступны для поиска по словам; смысловые индексы готовятся отдельно.')}</p>}
      <details open={['completed', 'failed'].includes(job.state)}><summary>{t('Отчёт импорта')}</summary>
        <dl className="diagnostics">
          {([['processed', 'Обработано'], ['added', 'Новые'], ['unchanged', 'Без изменений'], ['updated', 'Обновлённые редакции'], ['conflicts', 'Конфликты']] as const)
            .map(([key, label]) => <Fragment key={key}><dt>{t(label)}</dt><dd>{job[key]}</dd></Fragment>)}
          <dt>{t('Недоступные вложения')}</dt><dd>{job.missing_media + job.invalid_media}</dd>
        </dl>
      </details>
      {job.error && <p className="error" role="alert">{t(job.error)}</p>}
      {job.warnings?.map(warning => <p className="warning-note" key={warning}>{t(warning)}</p>)}
      <div className="dialog-actions">
        {['queued', 'running'].includes(job.state) && <button className="secondary" disabled={op.busy} onClick={() => control('pause')}>{t('Пауза')}</button>}
        {['paused', 'interrupted', 'failed'].includes(job.state) && <button className="primary" disabled={op.busy} onClick={() => control('resume')}>{t('Продолжить')}</button>}
        {['queued', 'running', 'paused', 'interrupted'].includes(job.state) && <button className="secondary" disabled={op.busy} onClick={() => control('cancel')}>{t('Отменить')}</button>}
      </div>
    </> : <p role="status">{t('Загружаем…')}</p>}
    {op.error && <p className="error" role="alert">{t(op.error)}</p>}
  </section>;
}
