import { useEffect, useState } from 'react';
import { api } from './api';
import { getLanguage, t } from './i18n';
import { useDialogOperation } from './useDialogOperation';
import ModelCard from './ModelCard';
import { ModelDeviceSummary } from './ModelSettings';

type Status = { ready?: boolean; enabled: boolean; preparation_state: string; error: string | null;
  profile?: string; model_id?: string; license?: string; source?: string;
  profiles?: { profile: string; model_id: string; revision?: string; available?: boolean }[];
  path: string; device: string; download_bytes: number; download_completed_bytes: number };

export default function RerankPanel({ onPerformance }: { onPerformance: () => void }) {
  const [status, setStatus] = useState<Status | null>(null);
  const [enabled, setEnabled] = useState(false);
  const [profile, setProfile] = useState('giga');
  const [profileDirty, setProfileDirty] = useState(false);
  const [enabledDirty, setEnabledDirty] = useState(false);
  const dirty = profileDirty || enabledDirty;
  const [offline, setOffline] = useState(false);
  const [loadError, setLoadError] = useState('');
  const op = useDialogOperation();
  useEffect(() => {
    if (op.busy) return;
    let alive = true;
    let pending = false;
    const controller = new AbortController();
    const read = () => {
      if (pending) return; pending = true;
      void api<Status>('/api/rerank', { signal: controller.signal }).then(value => {
        if (alive) { setStatus(value); setLoadError('');
          if (!enabledDirty) setEnabled(value.enabled);
          if (!profileDirty) setProfile(value.profile ?? 'giga');
        }
      }).catch(error => { if (alive) setLoadError(error.message); }).finally(() => { pending = false; });
    };
    read(); const timer = window.setInterval(read, 2000);
    return () => { alive = false; controller.abort(); window.clearInterval(timer); };
  }, [op.busy, profileDirty, enabledDirty]);
  const activePreparing = ['downloading', 'preparing'].includes(status?.preparation_state ?? '');
  const changedModel = profile !== (status?.profile ?? 'giga');
  const preparing = activePreparing && !changedModel;
  const ready = !changedModel && Boolean(status?.ready ?? status?.preparation_state === 'ready');
  const jina = profile === 'jina-v2';
  const custom = profile.startsWith('custom:');
  const customLabel = 'Пользовательское переранжирование';
  const checkboxLabel = custom ? customLabel : jina ? 'Переранжирование Jina' : 'Переранжирование Giga';
  const profiles = status?.profiles ?? [{ profile: 'giga', model_id: 'Giga' }, { profile: 'jina-v2', model_id: 'Jina v2' }];
  const modelName = (value: string) => value.startsWith('custom:') ? (profiles.find(p => p.profile === value)?.model_id ?? value) : value === 'jina-v2' ? 'Jina v2' : 'Giga';
  return <ModelCard title={custom ? customLabel : jina ? 'Уточнение результатов · Jina' : 'Уточнение результатов · Giga'} purpose="Дополнительно сортирует до 50 текстовых и до 50 OCR-фрагментов. Индекс не перестраивается. Поиск может занять больше времени."
    ready={ready} preparing={preparing} error={op.error || loadError || (!changedModel ? status?.error : null)}
    details={<div className="model-paths"><span>{t('Папка загрузки модели')}</span><code>{changedModel ? t('Сначала сохраните выбор модели.') : status?.path}</code>
      {!changedModel && status?.model_id && <code>{status.model_id}</code>}
    </div>}
    device={<ModelDeviceSummary role="e5" queryOnly onPerformance={onPerformance} />}
    download={status && !changedModel ? { completed: status.download_completed_bytes, total: status.download_bytes } : undefined}
    options={<>
      <label>{t('Модель переранжирования')}<select aria-label={t('Модель переранжирования')} value={profile} disabled={!status || op.busy || activePreparing}
        onChange={event => { setProfile(event.target.value); setProfileDirty(event.target.value !== (status?.profile ?? 'giga')); }}>
        {profiles.map(value => <option value={value.profile} key={value.profile}>{value.profile.startsWith('custom:')
          ? `${value.model_id}${value.revision ? ' · ' + value.revision : ''}${value.available === false ? ' · ' + t('Недоступно') : ''}`
          : value.profile === 'jina-v2' ? `Jina v2 · ${t('Английский')}` : `Giga · ${t('Русский')}`}</option>)}
      </select></label>
      <small>{getLanguage() === 'en' ? t('Для английского рекомендуется Jina; выбор модели не меняется вместе с языком интерфейса.')
        : t('Для русского рекомендуется Giga; выбор модели не меняется вместе с языком интерфейса.')}</small>
      {jina && <small>{t('Jina v2: лицензия CC-BY-NC-4.0, только некоммерческое использование.')}{' '}
        <a href="https://huggingface.co/jinaai/jina-reranker-v2-base-multilingual" target="_blank" rel="noreferrer">{t('Условия модели')}</a></small>}
      {changedModel && <small>{t('Сначала сохраните выбор модели.')}</small>}
      {changedModel && activePreparing && <small>{t('Сейчас подготавливается модель: {p0}.', { p0: modelName(status?.profile ?? 'giga') })}</small>}
      <label className="check-label"><input type="checkbox" aria-label={t(checkboxLabel)} checked={enabled}
        disabled={!status || op.busy || (!ready && !enabled)} onChange={event => { setEnabled(event.target.checked); setEnabledDirty(event.target.checked !== status?.enabled); }} />{t(checkboxLabel)}</label>
      {!custom && <label className="check-label"><input type="checkbox" checked={offline} disabled={op.busy || preparing} onChange={event => setOffline(event.target.checked)} />{t('Только локальный кэш моделей')}</label>}
    </>}
    actions={<>
      <button className="secondary" disabled={!dirty || op.busy || !status || (activePreparing && changedModel)} onClick={() => void op.run(async current => {
        const changes: Record<string, unknown> = {};
        if (profileDirty && changedModel) changes.rerank_profile = profile;
        if (enabledDirty && enabled !== status?.enabled) changes.giga_rerank_enabled = enabled;
        if (Object.keys(changes).length) await api('/api/settings', { method: 'PATCH', body: JSON.stringify(changes) });
        const value = await api<Status>('/api/rerank'); if (current()) { setStatus(value); setEnabled(value.enabled); setProfile(value.profile ?? 'giga'); setProfileDirty(false); setEnabledDirty(false); }
      })}>{t('Сохранить настройки модели')}</button>
      <button className="primary" disabled={!status || op.busy || preparing || ready || changedModel} onClick={() => void op.run(async current => {
        const value = await api<Status>('/api/rerank/prepare', { method: 'POST', body: JSON.stringify({ offline: custom || offline }) });
        if (current()) setStatus(value);
      })}>{t(custom ? 'Подготовить адаптер' : jina ? 'Подготовить Jina' : 'Подготовить Giga')}</button>
    </>}>
    <p>{custom ? t('Локальный Python-адаптер') : `${jina ? 'Jina v2' : 'Giga'} · ${t('Встроенная загрузка')}`}</p>
    {custom && <small>{t('Выбор модели не меняет индекс. Сохраните выбор, подготовьте адаптер и включите переранжирование.')}</small>}
    {status && !custom && !changedModel && <small>{t('Размер загрузки: {p0} МиБ', { p0: (status.download_bytes / 1024 ** 2).toFixed(0) })}</small>}
  </ModelCard>;
}
