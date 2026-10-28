import { createContext, useContext, useEffect, useState } from 'react';
import type { ReactNode } from 'react';
import { api } from './api';
import { t } from './i18n';

export type ModelRole = 'e5' | 'clip' | 'ocr';
export type Device = 'cpu' | 'auto' | 'gpu' | 'hybrid';
export type ModelSettings = { device: Device; search_device?: Device; engine?: 'tesseract' | 'paddle'; paths: string[]; profile_paths?: Record<string, string | string[]>; profile_names?: Record<string, string> };
type Models = Record<ModelRole, ModelSettings>;
const Context = createContext<{ models: Models | null; update: (role: ModelRole, model: ModelSettings) => void }>({ models: null, update: () => {} });

export function ModelSettingsProvider({ children }: { children: ReactNode }) {
  const [models, setModels] = useState<Models | null>(null);
  const [error, setError] = useState('');
  useEffect(() => {
    let alive = true;
    api<Models>('/api/models').then(value => { if (alive) setModels(value); })
      .catch(error => { if (alive) setError(error.message); });
    return () => { alive = false; };
  }, []);
  return <Context.Provider value={{ models, update: (role, value) => setModels(previous => previous ? { ...previous, [role]: value } : previous) }}>
    {error && <p className="error" role="alert">{t(error)}</p>}{children}
  </Context.Provider>;
}

export const useModelSettings = () => useContext(Context);

export function ModelPaths({ role, profile }: { role: ModelRole; profile?: string }) {
  const { models } = useModelSettings();
  const model = models?.[role];
  const selected = profile ? model?.profile_paths?.[profile] : null;
  const paths = selected ? (Array.isArray(selected) ? selected : [selected]) : model?.paths;
  return <div className="model-paths"><span>{t('Папка загрузки модели')}</span>
    {paths?.map(path => <code key={path}>{path}</code>) ?? <span>{t('Проверяем…')}</span>}</div>;
}

export function ModelDeviceSummary({ role, onPerformance, queryOnly = false }: { role: ModelRole; onPerformance: () => void; queryOnly?: boolean }) {
  const { models } = useModelSettings();
  const settings = models?.[role];
  const display = (value?: Device) => value === 'hybrid' ? 'CPU + GPU' : value === 'auto' ? t('Авто') : value?.toUpperCase() ?? '…';
  return <div className="model-device-summary"><span>{queryOnly ? t('Поиск: {p0}', { p0: display(settings?.search_device) })
    : role === 'ocr' ? t('Распознавание: {p0}', { p0: display(settings?.device) })
      : t('Индексация: {p0} · Поиск: {p1}', { p0: display(settings?.device), p1: display(settings?.search_device) })}</span>
    <button type="button" className="text-button" onClick={onPerformance}>{t('Настроить производительность')}</button></div>;
}
