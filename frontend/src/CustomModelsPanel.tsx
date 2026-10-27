import { useEffect, useRef, useState } from 'react';
import { api } from './api';
import { t } from './i18n';
import type { SemanticStatus } from './types';
import { useDialogOperation } from './useDialogOperation';
import { useModelSettings } from './ModelSettings';
import type { ModelSettings } from './ModelSettings';

type Summary = { id: string; role: string; identity: string; runtime: { name: string; version: string }; activation_supported?: boolean };
type Job = { id: string; state: string; action: string; role: string; error: string | null;
  report: (Summary & { registered?: boolean }) | null;
  progress: { phase: string; completed: number; total: number | null; unit: string } | null };
type Catalog = { models: Summary[]; errors: string[]; job: Job | null };
const terminal = new Set(['inspected', 'passed', 'failed', 'cancelled']);
const roles = [['text_embedding', 'Текстовые векторы'], ['image_embedding', 'Векторы изображений'],
  ['reranker', 'Переранжирование'], ['ocr', 'Распознавание текста'], ['asr', 'Распознавание аудио']] as const;
const states: Record<string, string> = { pending: 'В очереди', running: 'Проверяем…', saving: 'Сохраняем…',
  inspected: 'Manifest и файлы проверены. Код не запускался.', passed: 'Проверка адаптера завершена.',
  failed: 'Ошибка', cancelled: 'Проверка отменена.' };

export default function CustomModelsPanel({ onSemantic }: { onSemantic: (value: SemanticStatus) => void }) {
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [role, setRole] = useState('text_embedding');
  const [manifest, setManifest] = useState('');
  const [python, setPython] = useState('');
  const [device, setDevice] = useState('cpu');
  const [job, setJob] = useState<Job | null>(null);
  const [inspection, setInspection] = useState<{ identity: string; manifest: string; role: string } | null>(null);
  const [error, setError] = useState('');
  const [reload, setReload] = useState(0);
  const owned = useRef<string | null>(null);
  const refreshed = useRef<string | null>(null);
  const callback = useRef(onSemantic); callback.current = onSemantic;
  const { update } = useModelSettings();
  const modelUpdate = useRef(update); modelUpdate.current = update;
  const revision = useRef(0);
  const op = useDialogOperation();
  const active = Boolean(job && !terminal.has(job.state));
  useEffect(() => {
    let alive = true;
    const current = revision.current;
    api<Catalog>('/api/adapters').then(value => { if (alive) { setCatalog(value); if (revision.current === current) setJob(value.job); setError(''); } })
      .catch(reason => { if (alive) setError(reason.message); });
    return () => { alive = false; };
  }, [reload]);
  useEffect(() => {
    if (!job || !active) return;
    let alive = true; let timer: ReturnType<typeof setTimeout>;
    const id = job.id;
    const poll = async () => {
      try {
        const value = await api<Job>(`/api/adapters/jobs/${encodeURIComponent(id)}`);
        if (!alive) return;
        setJob(value); setError('');
        if (value.state === 'inspected' && value.report && owned.current === id) setInspection({ identity: value.report.identity, manifest, role });
        if (terminal.has(value.state)) return;
      } catch (reason) { if (alive) setError(reason instanceof Error ? reason.message : String(reason)); }
      if (alive) timer = setTimeout(poll, 700);
    };
    timer = setTimeout(poll, 200);
    return () => { alive = false; clearTimeout(timer); };
  }, [job?.id, active, manifest, role]);
  useEffect(() => {
    if (!job?.report?.registered || refreshed.current === job.id) return;
    let alive = true;
    Promise.all([api<Catalog>('/api/adapters'), api<SemanticStatus>('/api/semantic'), api<{ e5: ModelSettings }>('/api/models')]).then(([models, semantic, settings]) => {
      if (alive) { refreshed.current = job.id; setCatalog(models); callback.current(semantic); modelUpdate.current('e5', settings.e5); setError(''); }
    }).catch(reason => { if (alive) setError(reason.message); });
    return () => { alive = false; };
  }, [job?.id, job?.report?.registered, reload]);
  const change = (value: string, field: 'manifest' | 'role') => {
    if (field === 'manifest') setManifest(value); else setRole(value);
    setInspection(null); setError('');
  };
  const start = (action: 'inspect' | 'validate') => void op.run(async current => {
    revision.current++;
    const value = await api<Job>('/api/adapters/jobs', { method: 'POST', body: JSON.stringify({ action,
      role, manifest: manifest.trim(), ...(action === 'validate' ? { python: python.trim(), device, expected_identity: inspection?.identity } : {}) }) });
    if (current()) { owned.current = value.id; setJob(value); setError('');
      if (value.state === 'inspected' && value.report) setInspection({ identity: value.report.identity, manifest, role }); }
  });
  const checked = inspection?.manifest === manifest && inspection.role === role;
  return <details className="custom-models"><summary>{t('Добавить свою модель')}</summary>
    <p>{t('Python-адаптер запускается в отдельном окружении с правами пользователя. Используйте доверенный локальный код.')}</p>
    <label>{t('Роль адаптера')}<select aria-label={t('Роль адаптера')} value={role} disabled={active || op.busy} onChange={event => change(event.target.value, 'role')}>
      {roles.map(([value, label]) => <option value={value} key={value}>{t(label)}</option>)}
    </select></label>
    {!['text_embedding', 'reranker'].includes(role) && <p className="warning">{t('Для этой роли доступны проверка и регистрация. Подключение к поиску пока реализовано для текста и переранжирования.')}</p>}
    <label>{t('Путь к manifest адаптера')}<input aria-label={t('Путь к manifest адаптера')} value={manifest} disabled={active || op.busy} onChange={event => change(event.target.value, 'manifest')} /></label>
    <button type="button" className="secondary" disabled={!manifest.trim() || active || op.busy || !catalog} onClick={() => start('inspect')}>{t('Проверить manifest и файлы')}</button>
    <small>{t('Эта проверка не запускает код адаптера.')}</small>
    <label>{t('Python отдельного окружения')}<input aria-label={t('Python отдельного окружения')} value={python} disabled={active || op.busy} onChange={event => setPython(event.target.value)} /></label>
    <label>{t('Устройство проверки')}<select aria-label={t('Устройство проверки')} value={device} disabled={active || op.busy} onChange={event => setDevice(event.target.value)}><option value="cpu">CPU</option><option value="gpu">GPU</option></select></label>
    <div className="model-actions"><button type="button" className="primary" disabled={!checked || !python.trim() || active || op.busy} onClick={() => start('validate')}>{t('Запустить проверку и добавить')}</button>
      {active && <button type="button" className="secondary" disabled={op.busy || job?.state === 'saving'} onClick={() => void op.run(async current => {
        revision.current++;
        const value = await api<Job>(`/api/adapters/jobs/${encodeURIComponent(job!.id)}/cancel`, { method: 'POST' });
        if (current()) setJob(value);
      })}>{t('Отменить проверку')}</button>}
    </div>
    {job && <p role="status">{t(states[job.state] ?? 'Проверяем…')}{job.report && <> · {job.report.id}</>}</p>}
    {active && <progress aria-label={t('Проверка адаптера')} value={job?.progress?.total ? job.progress.completed : undefined} max={job?.progress?.total ?? undefined} />}
    {job?.report?.registered && <p>{t(job.report.role === 'text_embedding' ? 'Модель добавлена. Выберите её в карточке текста; смена индекса требует подтверждения.' : job.report.role === 'reranker' ? 'Модель добавлена. Выберите её в карточке переранжирования, подготовьте и включите.' : 'Адаптер сохранён. Его роль пока не подключена к индексации и поиску.')}</p>}
    {(error || op.error || job?.error) && <p className="error" role="alert">{t(error || op.error || job?.error || '')}</p>}
    {error && <button type="button" className="text-button" onClick={() => setReload(value => value + 1)}>{t('Повторить')}</button>}
    {catalog?.errors.map((value, i) => <p className="warning" key={i}>{t(value)}</p>)}
    {catalog && <ul>{catalog.models.map(model => <li key={model.identity}>{model.id} · {t(roles.find(([value]) => value === model.role)?.[1] ?? model.role)} · {model.runtime.name}</li>)}</ul>}
  </details>;
}
