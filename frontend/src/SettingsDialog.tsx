import type { ReactNode } from 'react';
import Dialog from './Dialog';
import LanguageSwitch from './LanguageSwitch';
import { useLanguage } from './i18n';

export default function SettingsDialog({ children, titleId, onClose, wide = false }: { children: ReactNode; titleId: string; onClose: () => void; wide?: boolean }) {
  const [language, onLanguage] = useLanguage();
  return <Dialog titleId={titleId} onClose={onClose} className={`settings-overlay${wide ? ' settings-center' : ''}`}
    controls={<LanguageSwitch value={language} onChange={onLanguage} />}>{children}</Dialog>;
}
