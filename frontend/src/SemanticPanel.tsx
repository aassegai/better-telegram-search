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
    <h3 id="semantic-title">Смысловой поиск</h3>
    <p>Модель обрабатывает сообщения локально на CPU. При подготовке загружаются только файлы модели с Hugging Face.</p>
    {!status ? <p>Проверяем…</p> : !status.runtime_installed ?
      <p className="warning">Установите окружение поиска: <code>uv sync --locked --extra semantic</code>, затем перезапустите приложение.</p> : <>
        <p role="status">Готово сегментов: {status.ready_segments} / {status.total_segments}{status.paused ? ' · на паузе' : ''}</p>
        {status.works.map(work => <p key={work.state}>{work.state === 'failed' ? 'С ошибкой' : work.state === 'running' ? 'Индексируется' : 'В очереди'}: {work.count} · фрагменты {work.chunks_done} / {work.chunks_total}</p>)}
        {status.estimated_remaining_seconds != null && <p>Оценка для уже измеренных фрагментов: около {Math.ceil(status.estimated_remaining_seconds / 60)} мин.</p>}
        {preparing && <div><p>{status.preparation_state === 'downloading' ? 'Загружается модель' : 'Проверяется модель'}</p>
          <progress aria-label="Загрузка модели" value={status.download_completed_bytes} max={Math.max(1, status.download_total_bytes)} />
          <p>{(status.download_completed_bytes / 1024 ** 2).toFixed(0)} / {(status.download_total_bytes / 1024 ** 2).toFixed(0)} МиБ</p></div>}
        {status.error && <p className="warning" role="alert">{status.error}</p>}
        <div className="job-actions">
          {status.enabled === 1 && <button disabled={op.busy || preparing} onClick={() => operate(`/api/semantic/${status.paused ? 'resume' : 'pause'}`)}>{status.paused ? 'Продолжить индексирование' : 'Пауза индексирования'}</button>}
          {status.works.some(work => work.state === 'failed') && <button disabled={op.busy || preparing} onClick={() => operate('/api/semantic/retry')}>Повторить задачи с ошибкой</button>}
          {status.enabled === 1 && <button disabled={op.busy || preparing} onClick={() => operate('/api/semantic/compact')}>Уплотнить векторный индекс</button>}
        </div>
        <label>Модель<select aria-label="Модель смыслового поиска" value={profile} disabled={op.busy || preparing} onChange={event => { chosenProfile.current = true; setProfile(event.target.value); }}>
          {status.profiles.map(item => <option value={item.profile} key={item.profile}>{item.profile === 'small' ? 'E5 small — рекомендуется' : 'E5 base — больше памяти'} · {(item.download_bytes / 1024 ** 2).toFixed(0)} МиБ</option>)}
        </select></label>
        <div className="semantic-options">
          <label><input type="checkbox" checked={reindex} disabled={op.busy || preparing} onChange={event => setReindex(event.target.checked)} />Разрешить полную переиндексацию</label>
          <label><input type="checkbox" checked={offline} disabled={op.busy || preparing} onChange={event => setOffline(event.target.checked)} />Использовать только локальный кэш</label>
          <label><input type="checkbox" checked={repair} disabled={op.busy || preparing} onChange={event => setRepair(event.target.checked)} />Заменить повреждённый набор модели</label>
        </div>
        <label>Локальная папка модели (необязательно)<input aria-label="Локальная папка модели" value={localBundle} disabled={op.busy || preparing} onChange={event => setLocalBundle(event.target.value)} placeholder="Папка с manifest.json и файлами модели" /></label>
        <button className="primary" disabled={op.busy || preparing || (!!status.profile && status.profile !== profile && !reindex)} onClick={() => operate('/api/semantic/prepare', { profile, reindex, offline, repair, local_bundle: localBundle || null })}>{op.busy ? 'Запускаем…' : 'Подготовить модель и индекс'}</button>
        {status.profile && status.profile !== profile && !reindex && <p>Для смены модели разрешите переиндексацию.</p>}
      </>}
    {op.error && <p className="error" role="alert">{op.error}</p>}
  </section>;
}
