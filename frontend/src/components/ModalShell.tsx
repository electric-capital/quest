/**
 * Shared chrome for portal-mounted modal dialogs.
 *
 * Every modal in the app renders the same skeleton: a full-screen overlay
 * portaled to document.body, a click on the overlay backdrop (but not on the
 * dialog itself) closes the modal, and Escape closes it. This component owns
 * that skeleton; each modal keeps its own class names and styling.
 *
 * Escape handling is a document-level keydown listener that is only attached
 * while the modal is open. A nested dialog that must swallow Escape (e.g. the
 * FileViewerModal save-to-drive form) can register a capture-phase listener
 * that stops propagation, which prevents this bubble-phase handler from firing.
 */

import { useCallback, useEffect, type ReactNode, type Ref } from 'react';
import { createPortal } from 'react-dom';

interface ModalShellProps {
  isOpen: boolean;
  /** Called on backdrop click and (unless overridden by onEscape) on Escape. */
  onClose: () => void;
  /** Class name(s) for the full-screen overlay element. */
  overlayClassName: string;
  /**
   * Optional wrapper element rendered inside the overlay. Omit to render
   * children directly inside the overlay (for modals that need sibling
   * elements at the overlay level).
   */
  modalClassName?: string;
  /** Ref to the modalClassName wrapper element (ignored without one). */
  modalRef?: Ref<HTMLDivElement>;
  /**
   * Override for the Escape key. Defaults to onClose. Pass null to disable
   * Escape handling entirely.
   */
  onEscape?: (() => void) | null;
  /** Extra keyboard handling on the overlay (e.g. arrow-key navigation). */
  onKeyDown?: React.KeyboardEventHandler<HTMLDivElement>;
  children: ReactNode;
}

export function ModalShell({
  isOpen,
  onClose,
  overlayClassName,
  modalClassName,
  modalRef,
  onEscape,
  onKeyDown,
  children,
}: ModalShellProps) {
  const escapeHandler = onEscape === undefined ? onClose : onEscape;

  useEffect(() => {
    if (!isOpen || !escapeHandler) return;

    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        escapeHandler();
      }
    };

    document.addEventListener('keydown', handleKeyDown);
    return () => document.removeEventListener('keydown', handleKeyDown);
  }, [isOpen, escapeHandler]);

  const handleOverlayClick = useCallback((e: React.MouseEvent) => {
    if (e.target === e.currentTarget) {
      onClose();
    }
  }, [onClose]);

  if (!isOpen) return null;

  return createPortal(
    <div
      className={overlayClassName}
      onClick={handleOverlayClick}
      onKeyDown={onKeyDown}
      role="dialog"
      aria-modal="true"
    >
      {modalClassName ? <div ref={modalRef} className={modalClassName}>{children}</div> : children}
    </div>,
    document.body
  );
}
