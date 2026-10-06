import { useEffect, useState } from 'react';
import type { FormEvent } from 'react';
import { api } from './api';
import { useDialogOperation } from './useDialogOperation';
import type { Chat, Preview } from './types';

export default function ImportDialog({ chats, initialPreview = null, onClose, onApplied }: {
  chats: Chat[]; initialPreview?: Preview | null; onClose: () => void; onApplied: () => Promise<void>;
}) {
  const [jsonPath, setJsonPath] = useState('');
  const [root, setRoot] = useState('');
  const [scope, setScope] = useState('default');
  const [target, setTarget] = useState('');
  const [createNew, setCreateNew] = useState(false);
  const [preferImported, setPreferImported] = useState(false);
  const [preview, setPreview] = useState<Preview | null>(initialPreview);
  const { busy, error, setError, guard, run } = useDialogOperation();

  useEffect(() => {
    if (busy || !preview || !['queued', 'running'].includes(preview.state)) return;
    let alive = true;
    const poll = async () => {
      const current = guard();
      try {
        const next = await api<Preview>(`/api/import-previews/${preview.id}`);
        if (alive && current()) setPreview(next);
      } catch (error) { if (alive && current()) setError(error instanceof Error ? error.message : 'Ошибка проверки.'); }
    };
    const timer = window.setInterval(() => void poll(), 750);
    return () => { alive = false; clearInterval(timer); };
  }, [preview?.id, preview?.state, busy]);

  async function inspect(event: FormEvent) {
    event.preventDefault();
    await run(async current => {
      const report = await api<Preview>('/api/import-previews', { method: 'POST', body: JSON.stringify({
        json_path: jsonPath, source_root: root || null, scope, target_chat_id: target || null,
        create_new: createNew, policy: preferImported ? 'prefer_imported' : 'preserve',
      }) });
      if (current()) setPreview(report);
    });
  }

  async function apply() {
    if (!preview) return;
    await run(async current => {
      await api(`/api/import-previews/${preview.id}/apply`, { method: 'POST' });
      if (!current()) return;
      await onApplied(); if (current()) onClose();
    });
  }

  async function control(action: string) {
    if (!preview) return;
    await run(async current => {
      const next = await api<Preview>(`/api/import-previews/${preview.id}/${action}`, { method: 'POST' });
      if (current()) setPreview(next);
    });
  }

  async function discard() {
    if (!preview) return;
    await run(async current => {
      await api(`/api/import-previews/${preview.id}/cancel`, { method: 'POST' });
      if (current()) { setPreview(null); setError(''); await onApplied(); }
    });
  }

  return <section className="modal" role="dialog" aria-modal="true" aria-labelledby="import-title">
    <button className="close" aria-label="Закрыть" disabled={busy} onClick={onClose}>×</button>
    <div className="eyebrow">ПОПОЛНИТЬ АРХИВ</div><h2 id="import-title">Импорт Telegram Desktop</h2>
    <p>Сначала проверим экспорт и покажем изменения. Сообщения попадут в базу после применения отчёта. Фотографии останутся в папке источника.</p>
    {error && <div className="error" role="alert">{error}</div>}
    {!preview ? <form onSubmit={inspect} className="import-form">
      <label>Путь к JSON<input required value={jsonPath} placeholder="/папка/экспорта/result.json" onChange={event => setJsonPath(event.target.value)} /></label>
      <label>Папка экспорта (необязательно)<input value={root} placeholder="По умолчанию — папка рядом с JSON" onChange={event => setRoot(event.target.value)} /></label>
      <label>Область аккаунта<input required value={scope} onChange={event => setScope(event.target.value)} /><small>Для разных аккаунтов укажите разные значения.</small></label>
      <label>Диалог для обновления<select value={target} onChange={event => {
        setTarget(event.target.value); const chat = chats.find(chat => chat.id === event.target.value); if (chat) setScope(chat.scope);
      }}><option value="">Определить по ID экспорта</option>{chats.map(chat => <option key={chat.id} value={chat.id}>{chat.name}</option>)}</select></label>
      <label className="check-label"><input type="checkbox" checked={createNew} onChange={event => setCreateNew(event.target.checked)} />Создать новый диалог, если в JSON нет ID</label>
      <label className="check-label"><input type="checkbox" checked={preferImported} onChange={event => setPreferImported(event.target.checked)} />Считать экспорт актуальным при неоднозначных редакциях</label>
      <button className="primary" disabled={busy}>{busy ? 'Проверяем источник…' : 'Проверить экспорт'}</button>
    </form> : <div className="preview-report">
      <p><strong>{preview.chat_name}</strong> · аккаунт: {preview.scope}</p>
      <p className="source-summary">Источник: {preview.root_relative_path}/{preview.json_relative_path}</p>
      <h3>{preview.state === 'ready' ? 'Отчёт готов' : preview.state === 'running' ? 'Проверяем сообщения…' : preview.state === 'queued' ? 'Проверка в очереди' : preview.state === 'paused' ? 'Проверка приостановлена' : 'Проверка остановлена'}</h3>
      <dl className="diagnostics"><dt>Обработано</dt><dd>{preview.processed}</dd><dt>Новые</dt><dd>{preview.added}</dd><dt>Без изменений</dt><dd>{preview.unchanged}</dd><dt>Обновлённые редакции</dt><dd>{preview.updated}</dd><dt>Конфликты</dt><dd>{preview.conflicts}</dd><dt>Недоступные вложения</dt><dd>{preview.missing_media + preview.invalid_media}</dd></dl>
      {preview.error && <div className="error" role="alert">{preview.error}</div>}
      {preview.warnings.map(warning => <p className="warning-note" key={warning}>{warning}</p>)}
      {preview.conflicts > 0 && <p>Конфликтующие версии пока сохранятся отдельно. После импорта вы сможете сравнить их и выбрать нужную.</p>}
      <div className="dialog-actions">
        {preview.state === 'ready' && <button className="primary" disabled={busy} onClick={() => void apply()}>{busy ? 'Применяем…' : 'Применить изменения'}</button>}
        {['running', 'queued'].includes(preview.state) && <button disabled={busy} onClick={() => void control('pause')}>Пауза</button>}
        {['paused', 'interrupted'].includes(preview.state) && <button disabled={busy} onClick={() => void control('resume')}>Продолжить проверку</button>}
        <button disabled={busy} onClick={() => void discard()}>Проверить другой экспорт</button>
      </div>
    </div>}
  </section>;
}
