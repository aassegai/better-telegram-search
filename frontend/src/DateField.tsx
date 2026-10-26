import { useRef } from 'react';
import { t } from './i18n';
import Icon from './Icon';

export default function DateField({ label, value, onChange }: { label: string; value: string; onChange: (value: string) => void }) {
  const input = useRef<HTMLInputElement>(null);
  const open = () => {
    const field = input.current;
    if (!field) return;
    field.focus();
    try { field.showPicker?.(); } catch { /* Keyboard input remains available if the browser blocks its picker. */ }
  };
  return <label>{t(label)}<span className="date-control">
    <input ref={input} type="date" aria-label={t(label)} value={value}
      onClick={open} onKeyDown={event => {
        if (['Enter', ' '].includes(event.key)) { event.preventDefault(); open(); }
      }} onChange={event => onChange(event.target.value)} />
    <button type="button" className="icon-button" aria-label={t('Открыть календарь: {p0}', { p0: t(label) })} onClick={open}><Icon name="calendar" /></button>
  </span></label>;
}
