import { useEffect, useRef, useState } from 'react';
import Markdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { api } from './api';
import { t } from './i18n';
import { useDialogOperation } from './useDialogOperation';

export type Update = {
  state: string; error: string | null; current_version: string; available_version: string | null;
  current_variant: 'cpu' | 'gpu'; variant: 'cpu' | 'gpu'; notes: string;
  completed_bytes: number; total_bytes: number; install_supported: boolean; gpu_build_supported: boolean;
};
const states: Record<string, string> = {
  idle: 'Обновления ещё не проверялись.', checking: 'Проверяем обновления…', up_to_date: 'Установлена последняя версия.',
  available: 'Доступна новая сборка.', downloading: 'Скачиваем обновление…', verifying: 'Проверяем сборку…',
  ready: 'Обновление проверено и готово к установке.', installing: 'Устанавливаем обновление и перезапускаем приложение…',
  updated: 'Приложение обновлено.', rolled_back: 'Восстановлена предыдущая версия.', failed: 'Не удалось обновить приложение.',
  recovery_required: 'Нужно восстановить прерванную установку.',
};
const active = new Set(['checking', 'downloading', 'verifying', 'installing', 'recovery_required']);

export default function UpdatePanel({ onRestart }: { onRestart: () => void }) {
  const [status, setStatus] = useState<Update | null>(null);
  const [variant, setVariant] = useState<'cpu' | 'gpu'>('cpu');
  const [notice, setNotice] = useState('');
  const revision = useRef(0);
  const mutation = useRef(false);
  const initialized = useRef(false);
  const op = useDialogOperation();
  useEffect(() => {
    let alive = true;
    let pending = false;
    const poll = async () => {
      if (pending || mutation.current) return;
      const requestRevision = revision.current;
      pending = true;
      try {
        const value = await api<Update>('/api/updates');
        if (!alive || requestRevision !== revision.current || mutation.current) return;
        setStatus(value); setNotice('');
        if (!initialized.current) { setVariant(value.current_variant); initialized.current = true; }

      } catch (error) {
        if (alive && requestRevision === revision.current && !mutation.current) setNotice(error instanceof Error ? error.message : t('Ошибка соединения.'));
      } finally { pending = false; }
    };
    void poll();
    const timer = window.setInterval(() => void poll(), 1000);
    return () => { alive = false; window.clearInterval(timer); };
  }, []);
  const action = (name: 'check' | 'download' | 'install') => void op.run(async current => {
    revision.current++; mutation.current = true;
    try {
      const value = await api<Update>(`/api/updates/${name}`, { method: 'POST',
        ...(name === 'check' ? { body: JSON.stringify({ variant }) } : {}),
      });
      if (name === 'install') onRestart();
      if (current()) setStatus(value);
    } finally { mutation.current = false; }
  });
  const busy = op.busy || Boolean(status && active.has(status.state));
  return <section className="update-panel">
    <h3>{t('Обновления приложения')}</h3>
    {status && <>
      <p>{t('Установлено: {p0} · {p1}', { p0: status.current_version, p1: status.current_variant.toUpperCase() })}</p>
      <label>{t('Вариант сборки')}<select aria-label={t('Вариант сборки')} value={variant} disabled={busy} onChange={event => { revision.current++; setVariant(event.target.value as 'cpu' | 'gpu'); }}>
        <option value="cpu">CPU</option>{status.gpu_build_supported && <option value="gpu">{t('GPU · NVIDIA CUDA')}</option>}
      </select></label>
      <p role="status">{t(states[status.state] ?? status.state)}</p>
      {status.available_version && <p>{t('Доступно: {p0} · {p1}', { p0: status.available_version, p1: status.variant.toUpperCase() })}</p>}
      {status.total_bytes > 0 && <div className="update-progress"><progress max={status.total_bytes} value={status.completed_bytes} aria-label={t('Скачивание обновления')} />
        <span>{t('{p0} / {p1} МиБ', { p0: (status.completed_bytes / 1024 ** 2).toFixed(1), p1: (status.total_bytes / 1024 ** 2).toFixed(1) })}</span></div>}
      {status.notes && <details><summary>{t('Что изменилось')}</summary><div className="release-notes">
        <Markdown remarkPlugins={[remarkGfm]} skipHtml disallowedElements={['img']}
          urlTransform={url => /^https?:\/\//i.test(url) ? url : ''}
          components={{ a: ({ href, children }) => href ? <a href={href} target="_blank" rel="noopener noreferrer">{children}</a> : <span>{children}</span> }}>
          {status.notes}
        </Markdown>
      </div></details>}
      <div className="job-actions">
        <button disabled={busy} onClick={() => action('check')}>{t('Проверить обновления')}</button>
        <button disabled={busy || variant !== status.variant || !status.available_version || !['available', 'failed'].includes(status.state)} onClick={() => action('download')}>{t('Скачать обновление')}</button>
        <button disabled={busy || variant !== status.variant || status.state !== 'ready' || !status.install_supported} onClick={() => action('install')}>{t('Обновить и перезапустить')}</button>
      </div>
      <p>{t('Обновление загружается из GitHub Releases и проверяется перед установкой. Переписки, модели и настройки сохраняются. При неудачном запуске возвращается предыдущая версия.')}</p>
      {!status.install_supported && <p>{t('Автоустановка доступна только в готовой сборке приложения. Из исходников обновляйте проект через Git и uv.')}</p>}
      {status.error && <p className="error" role="alert">{t(status.error)}</p>}
    </>}
    {notice && <p className="error" role="alert">{t(notice)}</p>}{op.error && <p className="error" role="alert">{t(op.error)}</p>}
  </section>;
}
