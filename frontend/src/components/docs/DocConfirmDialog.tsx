/**
 * Confirm dialog of the doc viewer: ModalShell chrome with a title, body
 * paragraphs, an inline error and Cancel / confirm buttons. The header's
 * Delete confirm uses it.
 */

import type { ReactNode } from 'react';
import { ModalShell } from '../ModalShell';
import './DocConfirmDialog.css';

export type DocConfirmTone = 'default' | 'warning' | 'danger';

export interface DocConfirmDialogProps {
  isOpen: boolean;
  title: string;
  /** Body paragraphs. */
  children: ReactNode;
  confirmLabel: string;
  /** Confirm button label while `busy`. */
  busyLabel?: string;
  tone?: DocConfirmTone;
  busy: boolean;
  error: string | null;
  onConfirm: () => void;
  onClose: () => void;
}

export function DocConfirmDialog({
  isOpen,
  title,
  children,
  confirmLabel,
  busyLabel,
  tone = 'default',
  busy,
  error,
  onConfirm,
  onClose,
}: DocConfirmDialogProps) {
  // A request in flight cannot be called back: keep the dialog up until it
  // settles instead of letting Escape / a backdrop click hide it.
  const close = busy ? () => {} : onClose;
  return (
    <ModalShell
      isOpen={isOpen}
      onClose={close}
      overlayClassName="doc-dialog-overlay"
      modalClassName="doc-dialog"
    >
      <h2 className="doc-dialog-title">{title}</h2>
      <div className="doc-dialog-body">{children}</div>
      {error && (
        <div className="doc-dialog-error" role="alert">
          {error}
        </div>
      )}
      <div className="doc-dialog-actions">
        <button
          type="button"
          className="doc-dialog-button doc-dialog-cancel"
          onClick={close}
          disabled={busy}
        >
          Cancel
        </button>
        <button
          type="button"
          className={`doc-dialog-button doc-dialog-confirm doc-dialog-confirm--${tone}`}
          onClick={onConfirm}
          disabled={busy}
        >
          {busy && busyLabel ? busyLabel : confirmLabel}
        </button>
      </div>
    </ModalShell>
  );
}
