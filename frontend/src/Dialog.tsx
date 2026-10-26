import { useLayoutEffect, useRef } from 'react';
import type { ReactNode } from 'react';
import { t } from './i18n';
import Icon from './Icon';

type Props = {
  children: ReactNode;
  onClose: () => void;
  titleId?: string;
  label?: string;
  closeLabel?: string;
  className?: string;
  dismissOnBackdrop?: boolean;
  controls?: ReactNode;
};

export default function Dialog({ children, onClose, titleId, label, closeLabel, className = '', dismissOnBackdrop = false, controls }: Props) {
  const ref = useRef<HTMLDialogElement>(null);
  useLayoutEffect(() => {
    const dialog = ref.current!;
    dialog.showModal();
    return () => { dialog.close(); };
  }, []);

  return <dialog ref={ref} className={`dialog-shell ${className}`} aria-labelledby={titleId} aria-label={label}
    onCancel={event => { event.preventDefault(); onClose(); }}
    onKeyDown={event => { if (event.key === 'Escape') event.stopPropagation(); }}
    onClick={event => { if (dismissOnBackdrop && event.target === event.currentTarget) onClose(); }}>
    <div className="dialog-frame">
      <button type="button" className="close dialog-close icon-button" aria-label={closeLabel ?? t('Закрыть')} onClick={onClose}><Icon name="close" /></button>
      {controls && <div className="dialog-toolbar">{controls}</div>}
      <section className="modal">{children}</section>
    </div>
  </dialog>;
}
