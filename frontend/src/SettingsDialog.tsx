import { useLayoutEffect, useRef } from 'react';
import type { ReactNode } from 'react';
import { t } from './i18n';

export default function SettingsDialog({ children, titleId, onClose }: { children: ReactNode; titleId: string; onClose: () => void }) {
  const close = useRef<HTMLButtonElement>(null);
  useLayoutEffect(() => {
    const previous = document.activeElement;
    close.current?.focus({ preventScroll: true });
    return () => {
      if (previous instanceof HTMLElement && previous.isConnected) previous.focus({ preventScroll: true });
    };
  }, []);

  return <div className="overlay settings-overlay">
    <div className="dialog-frame" role="dialog" aria-modal="true" aria-labelledby={titleId}
      onKeyDown={event => { if (event.key === 'Escape') { event.stopPropagation(); onClose(); } }}>
      <button ref={close} type="button" className="close dialog-close" aria-label={t('Закрыть')} onClick={onClose}>×</button>
      <section className="modal">{children}</section>
    </div>
  </div>;
}
