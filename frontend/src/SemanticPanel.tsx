import { t } from './i18n';
import { useEffect, useRef, useState } from 'react';
import { api } from './api';
import type { SemanticStatus } from './types';
import { useDialogOperation } from './useDialogOperation';
import type { Mutation } from './IndexCard';
import DevicePanel from './DevicePanel';

type Result = SemanticStatus | { semantic: SemanticStatus };
export default function SemanticPanel({ status, onChange, chatId, onStart, onEnd, pending }: Mutation & {
  status: SemanticStatus | null; onChange: (status: SemanticStatus) => void; chatId?: string;
}) {
  const [profile, setProfile] = useState(status?.profile || 'small');
  const chosenProfile = useRef(false);
  useEffect(() => {
    if (status?.profile && !chosenProfile.current) setProfile(status.profile);
  }, [status?.profile]);
  const [reindex, setReindex] = useState(false);
  const [offline, setOffline] = useState(false);
  const [repair, setRepair] = useState(false);
  const [localBundle, setLocalBundle] = useState('');
  const op = useDialogOperation();
  const busy = op.busy || Boolean(pending);
  const preparing = ['downloading', 'preparing'].includes(status?.preparation_state || '');
  const accept = (value: unknown) => {
    const result = value as Result;
    onChange('semantic' in result ? result.semantic : result);
  };
  const operate = (action: string, body?: object) => void op.run(async current => {
    onStart?.();
    try {
      const path = chatId ? `/api/chats/${encodeURIComponent(chatId)}/index/text/${action}` : `/api/semantic/${action}`;
      const result = await api<Result>(path, { method: 'POST', ...(body ? { body: JSON.stringify(body) } : {}) });
      if (current()) accept(result);
    } finally { onEnd?.(); }
  });
  const prepare = () => operate('prepare', { profile, reindex, offline, repair, local_bundle: localBundle || null });
  if (!status) return <p>{t('Проверяем…')}</p>;
  return <section className="model-setup semantic-panel">
    <h3>{t('Модель текста · E5')}</h3>
    <DevicePanel model="e5" profile={profile} onChange={async current => {
      const value = await api<SemanticStatus>('/api/semantic'); if (current()) onChange(value);
    }} />
    <p className="baseline-note">{t('Модели общие для всех диалогов. Прогресс, пауза и батчи находятся в меню диалога.')}</p>
    {status.backend?.warning && <p className="warning">{t(status.backend.warning)}</p>}
    {preparing && <div className="model-download"><progress aria-label={t('Загрузка модели')} value={status.download_completed_bytes} max={Math.max(1, status.download_total_bytes)} />
      <small>{(status.download_completed_bytes / 1024 ** 2).toFixed(0)} / {(status.download_total_bytes / 1024 ** 2).toFixed(0)}{t(' МиБ')}</small></div>}
    {status.error && <p className="warning" role="alert">{t(status.error)}</p>}
    {op.error && <p className="error" role="alert">{t(op.error)}</p>}
      <label>{t('Модель')}<select aria-label={t('Модель смыслового поиска')} value={profile} disabled={busy || preparing} onChange={event => { chosenProfile.current = true; setProfile(event.target.value); }}>
        {status.profiles.map(item => <option value={item.profile} key={item.profile}>{item.profile === 'small' ? t('E5 small — рекомендуется') : t('E5 base — больше памяти')} · {(item.download_bytes / 1024 ** 2).toFixed(0)}{t(' МиБ')}</option>)}
      </select></label>
      <div className="semantic-options">
        <label><input type="checkbox" checked={reindex} disabled={busy || preparing} onChange={event => setReindex(event.target.checked)} />{t('Разрешить переиндексацию всех диалогов при смене модели')}</label>
        <label><input type="checkbox" checked={offline} disabled={busy || preparing} onChange={event => setOffline(event.target.checked)} />{t('Использовать только локальный кэш')}</label>
        <label><input type="checkbox" checked={repair} disabled={busy || preparing} onChange={event => setRepair(event.target.checked)} />{t('Заменить повреждённый набор модели')}</label>
      </div>
      <label>{t('Локальная папка модели (необязательно)')}<input aria-label={t('Локальная папка модели')} value={localBundle} disabled={busy || preparing} onChange={event => setLocalBundle(event.target.value)} placeholder={t('Папка с manifest.json и файлами модели')} /></label>
      <button disabled={busy || preparing || !status.runtime_installed || (!!status.profile && status.profile !== profile && !reindex)} onClick={prepare}>{t('Подготовить модель текста')}</button>
      {status.profile && status.profile !== profile && !reindex && <p>{t('Для смены модели разрешите переиндексацию.')}</p>}
      {status.enabled === 1 && <button disabled={busy || preparing} onClick={() => operate('compact')}>{t('Уплотнить векторный индекс')}</button>}
  </section>;
}
