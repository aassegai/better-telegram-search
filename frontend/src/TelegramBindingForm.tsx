import { useState } from 'react';
import { api } from './api';
import { t } from './i18n';
import type { Chat } from './types';
import { useDialogOperation } from './useDialogOperation';

type Remote = { key: string; peer_type: string; peer_id: number; name: string };
type Page = { dialogs: Remote[]; next_cursor: string | null };
type Preview = { preview_token: string; revision: number | null; matched: number; edited: number; mismatches: number; unavailable: number; baseline_id: number; can_bind: boolean; typed_id_match: boolean };

export default function TelegramBindingForm({ chats, onChanged }: { chats: Chat[]; onChanged: () => Promise<void> }) {
  const [page, setPage] = useState<Page | null>(null);
  const [query, setQuery] = useState('');
  const [chosen, setChosen] = useState('');
  const [target, setTarget] = useState('');
  const [start, setStart] = useState(new Date().toISOString().slice(0, 10));
  const [preview, setPreview] = useState<Preview | null>(null);
  const [confirmed, setConfirmed] = useState(false);
  const [notice, setNotice] = useState('');
  const op = useDialogOperation();
  const reset = () => { setPreview(null); setConfirmed(false); setNotice(''); };
  const load = (cursor = '') => void op.run(async current => {
    const params = new URLSearchParams({ q: query, cursor });
    const result = await api<Page>(`/api/telegram/dialogs?${params}`);
    if (current()) { setPage(result); setChosen(''); reset(); }
  });
  const check = () => void op.run(async current => {
    reset();
    const result = await api<Preview>('/api/telegram/bindings/preview', { method: 'POST',
      body: JSON.stringify({ peer_key: chosen, chat_id: target || null, start_date: target ? null : start }) });
    if (current()) setPreview(result);
  });
  const bind = () => void op.run(async current => {
    if (!preview) return;
    await api('/api/telegram/bindings', { method: 'POST', body: JSON.stringify({ preview_token: preview.preview_token, expected_revision: preview.revision, confirm_account: confirmed }) });
    if (current()) {
      reset(); setChosen(''); setNotice(t('Диалог подключён. Получение новых сообщений запущено.'));
      await onChanged();
    }
  });
  return <section className="telegram-binding">
    <h3>{t('Выбрать диалог для синхронизации')}</h3>
    <div className="telegram-form"><label>{t('Фильтр диалогов на странице')}<input maxLength={200} value={query} disabled={op.busy} onChange={event => { setQuery(event.target.value); reset(); }} /></label></div>
    <div className="dialog-actions"><button disabled={op.busy} onClick={() => load()}>{t('Показать диалоги Telegram')}</button>
      {page?.next_cursor && <button disabled={op.busy} onClick={() => load(page.next_cursor!)}>{t('Следующая страница')}</button>}</div>
    {page && <div className="telegram-form">
      {!page.dialogs.length && <p>{t('На этой странице совпадений нет. Проверьте следующую страницу.')}</p>}
      <label>{t('Диалог Telegram')}<select aria-label={t('Диалог Telegram')} disabled={op.busy} value={chosen} onChange={event => { setChosen(event.target.value); reset(); }}>
        <option value="">{t('Выберите диалог')}</option>{page.dialogs.map(peer => <option key={peer.key} value={peer.key}>{peer.name || peer.key} · {peer.key}</option>)}
      </select></label>
      <label>{t('Локальный архив')}<select aria-label={t('Локальный архив')} disabled={op.busy} value={target} onChange={event => { setTarget(event.target.value); reset(); }}>
        <option value="">{t('Создать новый диалог')}</option>{chats.map(chat => <option key={chat.id} value={chat.id}>{chat.name} · {chat.scope}</option>)}
      </select></label>
      {!target && <label>{t('Загружать сообщения начиная с (UTC)')}<input type="date" value={start} disabled={op.busy} onChange={event => { setStart(event.target.value); reset(); }} /></label>}
      <div className="dialog-actions"><button disabled={op.busy || !chosen || (!target && !start)} onClick={check}>{t('Проверить привязку')}</button></div>
    </div>}
    {preview && <div className="telegram-preview" role="status">
      <p>{t('Совпало: {p0} · редакций: {p1} · расхождений: {p2} · недоступно: {p3}', { p0: preview.matched, p1: preview.edited, p2: preview.mismatches, p3: preview.unavailable })}</p>
      <p className="baseline-note">{target ? t('Старую историю до сообщения #{p0} берём из выгрузки; её полнота не проверена.', { p0: preview.baseline_id }) : t('Будут загружаться сообщения после выбранной даты. Более ранняя история не загружается.')}</p>
      {!preview.typed_id_match && target && <p className="warning">{t('В выгрузке нет распознанного типа и ID. Проверьте выбранный диалог особенно внимательно.')}</p>}
      {!preview.can_bind && <p className="error">{t('История не совпала. Привязка заблокирована: выберите правильный диалог.')}</p>}
      <label className="check-label"><input type="checkbox" checked={confirmed} disabled={op.busy || !preview.can_bind} onChange={event => setConfirmed(event.target.checked)} />{t('Подтверждаю, что это нужный аккаунт и диалог Telegram')}</label>
      <div className="dialog-actions"><button className="primary" disabled={op.busy || !confirmed || !preview.can_bind} onClick={bind}>{t('Подключить диалог')}</button></div>
    </div>}
    {notice && <p role="status">{t(notice)}</p>}{op.error && <p className="error" role="alert">{t(op.error)}</p>}
  </section>;
}
