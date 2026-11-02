import { useEffect, type ReactNode } from 'react';

interface ModalShellProps {
  title: string;
  onClose: () => void;
  children: ReactNode;
  /** Extra class on the dialog panel (e.g. width tuning per modal). */
  className?: string;
  /** When false, Escape leaves this dialog open (a dialog stacked on top handles it). */
  closeOnEscape?: boolean;
}

/** Shared overlay/dialog chrome for the account modals (profile, settings, friends). */
export default function ModalShell({
  title,
  onClose,
  children,
  className = '',
  closeOnEscape = true,
}: ModalShellProps) {
  useEffect(() => {
    if (!closeOnEscape) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose, closeOnEscape]);

  return (
    <div className="account-modal-overlay" role="presentation" onClick={onClose}>
      <div
        className={`account-modal ${className}`.trim()}
        role="dialog"
        aria-modal="true"
        aria-label={title}
        onClick={(e) => e.stopPropagation()}
      >
        <header className="account-modal__header">
          <h2 className="account-modal__title">{title}</h2>
          <button
            type="button"
            className="account-modal__close"
            onClick={onClose}
            aria-label={`Close ${title}`}
          >
            ×
          </button>
        </header>
        <div className="account-modal__body">{children}</div>
      </div>
    </div>
  );
}
