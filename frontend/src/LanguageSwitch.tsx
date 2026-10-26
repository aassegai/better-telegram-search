import { t } from './i18n';
import type { Language } from './i18n';

export default function LanguageSwitch({ value, onChange }: { value: Language; onChange: (language: Language) => void }) {
  return <div className="language-switch segmented-control" role="group" aria-label={t('Язык приложения')} onClick={event => {
    if (event.target === event.currentTarget) {
      const bounds = event.currentTarget.getBoundingClientRect();
      onChange(event.clientX < bounds.left + bounds.width / 2 ? 'ru' : 'en');
    }
  }}>
    {(['ru', 'en'] as const).map(language => <button key={language} type="button" aria-pressed={value === language}
      lang={language} onClick={() => onChange(language)} onKeyDown={event => {
        if (['ArrowLeft', 'Home'].includes(event.key)) { event.preventDefault(); onChange('ru'); }
        if (['ArrowRight', 'End'].includes(event.key)) { event.preventDefault(); onChange('en'); }
      }}>{language.toUpperCase()}</button>)}
  </div>;
}
