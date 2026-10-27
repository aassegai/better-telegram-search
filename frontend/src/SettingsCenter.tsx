import { useRef } from 'react';
import { api } from './api';
import { t } from './i18n';
import type { Chat, MediaStatus, SemanticStatus } from './types';
import ResourcePanel from './ResourcePanel';
import AnnPanel from './AnnPanel';
import DevicePanel from './DevicePanel';
import ChatPerformancePanel from './ChatPerformancePanel';
import SemanticPanel from './SemanticPanel';
import RerankPanel from './RerankPanel';
import MediaModelsPanel from './MediaModelsPanel';
import DatabasePanel from './DatabasePanel';
import UpdatePanel from './UpdatePanel';
import { ModelSettingsProvider } from './ModelSettings';

const sections = [['performance', 'Производительность'], ['models', 'Модели'], ['database', 'База'], ['updates', 'Обновления']] as const;
export type SettingsSection = typeof sections[number][0];
export const initialSettingsSection = (): SettingsSection => {
  try { const saved = localStorage.getItem('bts.settings-section'); return sections.some(([key]) => key === saved) ? saved as SettingsSection : 'performance'; }
  catch { return 'performance'; }
};

export default function SettingsCenter({ section, onSection, chats, scope, onScope, semantic, media, onSemantic, onMedia, onRestart }: {
  section: SettingsSection; onSection: (section: SettingsSection) => void; chats: Chat[];
  scope: string | null; onScope: (scope: string | null) => void;
  semantic: SemanticStatus | null; media: MediaStatus | null; onSemantic: (status: SemanticStatus) => void;
  onMedia: (status: MediaStatus) => void; onRestart: () => void;
}) {
  const visited = useRef(new Set([section]));
  visited.current.add(section);
  const choose = (section: SettingsSection) => {
    onSection(section);
    try { localStorage.setItem('bts.settings-section', section); } catch { /* Navigation also works without storage. */ }
  };
  const performance = () => { onScope(null); choose('performance'); };
  return <ModelSettingsProvider><div className="settings-layout">
    <div className="settings-navigation" role="tablist" aria-label={t('Разделы настроек')} aria-orientation="vertical">
      {sections.map(([key, label], index) => <button key={key} id={`settings-tab-${key}`} type="button" role="tab"
        aria-selected={section === key} aria-controls={`settings-panel-${key}`} tabIndex={section === key ? 0 : -1}
        onClick={() => choose(key)} onKeyDown={event => {
          const direction = ['ArrowDown', 'ArrowRight'].includes(event.key) ? 1 : ['ArrowUp', 'ArrowLeft'].includes(event.key) ? -1 : 0;
          const target = event.key === 'Home' ? 0 : event.key === 'End' ? sections.length - 1 : direction ? (index + direction + sections.length) % sections.length : null;
          if (target !== null) { event.preventDefault(); choose(sections[target][0]); document.getElementById(`settings-tab-${sections[target][0]}`)?.focus(); }
        }}>{t(label)}</button>)}
    </div>
    <div className="settings-content">
      {sections.map(([key]) => visited.current.has(key) && <section key={key} role="tabpanel" id={`settings-panel-${key}`}
        aria-labelledby={`settings-tab-${key}`} hidden={section !== key} tabIndex={0}>
        {key === 'performance' && <>
          <label className="performance-scope">{t('Область настройки')}<select aria-label={t('Область настройки')} value={scope ?? ''} onChange={event => onScope(event.target.value || null)}>
            <option value="">{t('Все диалоги')}</option>{chats.map(chat => <option key={chat.id} value={chat.id}>{chat.name}</option>)}
          </select></label>
          {scope ? <ChatPerformancePanel key={scope} chatId={scope} /> : <>
            <ResourcePanel />
            <AnnPanel />
            <p className="baseline-note">{t('Перед сменой устройств приостановите индексацию текста и медиа. Индекс можно продолжить на другом устройстве.')}</p>
            <DevicePanel model="e5" onChange={async current => { const value = await api<SemanticStatus>('/api/semantic'); if (current()) onSemantic(value); }} />
            {(['clip', 'ocr'] as const).map(model => <DevicePanel key={model} model={model} onChange={async current => { const value = await api<MediaStatus>('/api/media-index'); if (current()) onMedia(value); }} />)}
            <p className="baseline-note">{t('Giga использует устройство текстового поиска.')}</p>
          </>}
        </>}
        {key === 'models' && <>
          <p className="baseline-note">{t('Модели общие для всех диалогов. Язык интерфейса не меняет модель автоматически.')}</p>
          <SemanticPanel status={semantic} onChange={onSemantic} onPerformance={performance} />
          <RerankPanel onPerformance={performance} />
          <MediaModelsPanel media={media} onMediaChange={onMedia} onPerformance={performance} />
        </>}
        {key === 'database' && <DatabasePanel chats={chats} />}
        {key === 'updates' && <UpdatePanel onRestart={onRestart} />}
      </section>)}
    </div>
  </div></ModelSettingsProvider>;
}
