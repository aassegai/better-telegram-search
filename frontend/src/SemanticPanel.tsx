import { getLanguage, t } from './i18n';
import { useEffect, useRef, useState } from 'react';
import { api } from './api';
import type { SemanticStatus } from './types';
import { useDialogOperation } from './useDialogOperation';
import ModelCard from './ModelCard';
import { ModelDeviceSummary, ModelPaths } from './ModelSettings';

export default function SemanticPanel({ status, onChange, onPerformance }: {
  status: SemanticStatus | null; onChange: (status: SemanticStatus) => void; onPerformance: () => void;
}) {
  const [profile, setProfile] = useState(status?.profile || 'berta');
  const chosen = useRef(false);
  useEffect(() => { if (status?.profile && !chosen.current) setProfile(status.profile); }, [status?.profile]);
  const [reindex, setReindex] = useState(false);
  const [offline, setOffline] = useState(false);
  const [repair, setRepair] = useState(false);
  const [source, setSource] = useState('builtin');
  const [localBundle, setLocalBundle] = useState('');
  const op = useDialogOperation();
  if (!status) return <p role="status">{t('Проверяем…')}</p>;
  const preparing = ['downloading', 'preparing'].includes(status.preparation_state);
  const changing = Boolean(status.profile && status.profile !== profile);
  const ready = status.dense_available && status.profile === profile;
  const selected = status.profiles.find(item => item.profile === profile);
  const custom = profile.startsWith('custom:');
  const disabled = op.busy || preparing;
  const prepare = (rebuild = changing && reindex) => void op.run(async current => {
    const value = await api<SemanticStatus>('/api/semantic/prepare', { method: 'POST', body: JSON.stringify({
      profile, reindex: rebuild, offline: custom || offline, repair: !custom && repair, local_bundle: !custom && source === 'local' ? localBundle : null,
    }) });
    if (current()) onChange(value);
  });
  return <ModelCard title="Модель текста" purpose="Смысловой поиск по сообщениям и распознанному тексту."
    ready={ready} preparing={preparing} error={op.error || status.error}
    download={{ completed: status.download_completed_bytes, total: status.download_total_bytes }}
    details={<><ModelPaths role="e5" profile={profile} /><p>{custom ? t('Локальный Python-адаптер') : 'ONNX Runtime'} · {selected?.model_id ?? profile}</p></>}
    device={<ModelDeviceSummary role="e5" onPerformance={onPerformance} />}
    options={<>
      {changing && <label className="check-label"><input type="checkbox" checked={reindex} disabled={disabled} onChange={event => setReindex(event.target.checked)} />{t('Разрешить переиндексацию всех диалогов при смене модели')}</label>}
      {!custom && <details className="model-options"><summary>{t('Параметры подготовки')}</summary>
        <label className="check-label"><input type="checkbox" checked={offline} disabled={disabled} onChange={event => setOffline(event.target.checked)} />{t('Использовать только локальный кэш')}</label>
        <label className="check-label"><input type="checkbox" checked={repair} disabled={disabled} onChange={event => setRepair(event.target.checked)} />{t('Заменить повреждённый набор модели')}</label>
      </details>}
      {status.backend?.warning && <p className="warning">{t(status.backend.warning)}</p>}
    </>}
    actions={<>
      <button className={changing ? 'danger' : 'primary'} disabled={disabled || !status.runtime_installed || (changing && !reindex) || (ready && (custom || (!repair && source === 'builtin'))) || (!custom && source === 'local' && !localBundle.trim())}
        onClick={() => prepare()}>{changing ? t('Сменить модель и перестроить текст') : t('Подготовить модель текста')}</button>
      {ready && !changing && <button className="secondary" disabled={disabled} onClick={() => {
        if (window.confirm(t('Перестроить текстовый индекс всех диалогов? Сообщения и медиа сохранятся.'))) prepare(true);
      }}>{t('Перестроить текстовый индекс')}</button>}
    </>}>
    <label>{t('Модель')}<select aria-label={t('Модель смыслового поиска')} value={profile} disabled={disabled} onChange={event => { chosen.current = true; setProfile(event.target.value); setReindex(false); }}>
      {status.profile && ['small', 'base'].includes(status.profile) && <option value={status.profile} disabled>{status.profile === 'small' ? 'E5 small · legacy' : 'E5 base · legacy'}</option>}
      {status.profiles.filter(item => !['small', 'base'].includes(item.profile)).map(item => <option value={item.profile} key={item.profile}>{item.profile === 'berta' ? 'BERTA' : item.profile === 'ultrabert' ? 'UltraBERT-Embed-150M' : item.profile.startsWith('custom:') && item.revision ? `${item.model_id} · ${item.revision}` : item.model_id}</option>)}
    </select></label>
    <small>{getLanguage() === 'en' ? t('Для английского текста рекомендуется UltraBERT; язык интерфейса не меняет выбранную модель.')
      : t('Для русского текста рекомендуется BERTA; язык интерфейса не меняет выбранную модель.')}</small>
    {status.profile && ['small', 'base'].includes(status.profile) && <p className="warning">{t('Существующий индекс использует прежнюю модель. Его можно продолжать; переход на другую модель требует перестройки.')}</p>}
    {!status.runtime_installed && <p className="warning">{t('Установите ONNX runtime: uv sync --locked --extra semantic или --extra gpu.')}</p>}
    {custom ? <p>{t('Источник: проверенный локальный адаптер')}</p> : <label>{t('Источник модели')}<select aria-label={t('Источник текстовой модели')} value={source} disabled={disabled} onChange={event => setSource(event.target.value)}>
      <option value="builtin">{t('Встроенная загрузка')}</option><option value="local">{t('Локальная папка модели')}</option>
    </select></label>}
    {!custom && source === 'local' && <label>{t('Локальная папка модели')}<input aria-label={t('Локальная папка модели')} value={localBundle} disabled={disabled} onChange={event => setLocalBundle(event.target.value)} placeholder={t('Папка с manifest.json и файлами модели')} /></label>}
    {selected && !custom && <small>{t('Размер загрузки: {p0} МиБ', { p0: (selected.download_bytes / 1024 ** 2).toFixed(0) })}</small>}
  </ModelCard>;
}
