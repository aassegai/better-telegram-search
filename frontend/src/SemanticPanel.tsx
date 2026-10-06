import { t } from './i18n';
import { useEffect, useRef, useState } from 'react';
import { api } from './api';
import type { SemanticStatus } from './types';
import { useDialogOperation } from './useDialogOperation';

export default function SemanticPanel({ status, onChange }: {
  status: SemanticStatus | null; onChange: (status: SemanticStatus) => void;
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
  const preparing = ['downloading', 'preparing'].includes(status?.preparation_state || '');
  const operate = (path: string, body?: object) => void op.run(async current => {
    const result = await api<SemanticStatus>(path, {
      method: 'POST', ...(body ? { body: JSON.stringify(body) } : {}),
    });
    if (current()) onChange(result);
  });
  return <section className="semantic-panel" aria-labelledby="semantic-title">
    <h3 id="semantic-title">{t("Смысловой поиск")}</h3>
    <p>{t("Модель обрабатывает сообщения локально на CPU. При подготовке загружаются только файлы модели с Hugging Face.")}</p>
    {!status ? <p>{t("Проверяем…")}</p> : !status.runtime_installed ?
      <p className="warning">{t("Установите окружение поиска: ")}<code>uv sync --locked --extra semantic</code>{t(", затем перезапустите приложение.")}</p> : <>
        <p role="status">{t("Готово сегментов: ")}{status.ready_segments} / {status.total_segments}{status.paused ? t(' · на паузе') : ''}</p>
        {status.works.map(work => <p key={work.state}>{work.state === 'failed' ? t('С ошибкой') : work.state === 'running' ? t('Индексируется') : t('В очереди')}: {work.count}{t(" · фрагменты ")}{work.chunks_done} / {work.chunks_total}</p>)}
        {status.estimated_remaining_seconds != null && <p>{t("Оценка для уже измеренных фрагментов: около ")}{Math.ceil(status.estimated_remaining_seconds / 60)}{t(" мин.")}</p>}
        {preparing && <div><p>{status.preparation_state === 'downloading' ? t('Загружается модель') : t('Проверяется модель')}</p>
          <progress aria-label={t("Загрузка модели")} value={status.download_completed_bytes} max={Math.max(1, status.download_total_bytes)} />
          <p>{(status.download_completed_bytes / 1024 ** 2).toFixed(0)} / {(status.download_total_bytes / 1024 ** 2).toFixed(0)}{t(" МиБ")}</p></div>}
        {status.error && <p className="warning" role="alert">{t(status.error)}</p>}
        <div className="job-actions">
          {status.enabled === 1 && <button disabled={op.busy || preparing} onClick={() => operate(`/api/semantic/${status.paused ? 'resume' : 'pause'}`)}>{status.paused ? t('Продолжить индексирование') : t('Пауза индексирования')}</button>}
          {status.works.some(work => work.state === 'failed') && <button disabled={op.busy || preparing} onClick={() => operate('/api/semantic/retry')}>{t("Повторить задачи с ошибкой")}</button>}
          {status.enabled === 1 && <button disabled={op.busy || preparing} onClick={() => operate('/api/semantic/compact')}>{t("Уплотнить векторный индекс")}</button>}
        </div>
        <label>{t("Модель")}<select aria-label={t("Модель смыслового поиска")} value={profile} disabled={op.busy || preparing} onChange={event => { chosenProfile.current = true; setProfile(event.target.value); }}>
          {status.profiles.map(item => <option value={item.profile} key={item.profile}>{item.profile === 'small' ? t('E5 small — рекомендуется') : t('E5 base — больше памяти')} · {(item.download_bytes / 1024 ** 2).toFixed(0)}{t(" МиБ")}</option>)}
        </select></label>
        <div className="semantic-options">
          <label><input type="checkbox" checked={reindex} disabled={op.busy || preparing} onChange={event => setReindex(event.target.checked)} />{t("Разрешить полную переиндексацию")}</label>
          <label><input type="checkbox" checked={offline} disabled={op.busy || preparing} onChange={event => setOffline(event.target.checked)} />{t("Использовать только локальный кэш")}</label>
          <label><input type="checkbox" checked={repair} disabled={op.busy || preparing} onChange={event => setRepair(event.target.checked)} />{t("Заменить повреждённый набор модели")}</label>
        </div>
        <label>{t("Локальная папка модели (необязательно)")}<input aria-label={t("Локальная папка модели")} value={localBundle} disabled={op.busy || preparing} onChange={event => setLocalBundle(event.target.value)} placeholder={t("Папка с manifest.json и файлами модели")} /></label>
        <button className="primary" disabled={op.busy || preparing || (!!status.profile && status.profile !== profile && !reindex)} onClick={() => operate('/api/semantic/prepare', { profile, reindex, offline, repair, local_bundle: localBundle || null })}>{op.busy ? t('Запускаем…') : t('Подготовить модель и индекс')}</button>
        {status.profile && status.profile !== profile && !reindex && <p>{t("Для смены модели разрешите переиндексацию.")}</p>}
      </>}
    {op.error && <p className="error" role="alert">{t(op.error)}</p>}
  </section>;
}
