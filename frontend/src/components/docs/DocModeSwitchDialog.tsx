/**
 * Confirm dialogs of the doc viewer.
 *
 * DocModeSwitchDialog asks before a user doc switches between private and
 * public (spec 8.4; the private -> public text is verbatim) and calls
 * PUT /docs/{id}/mode on confirm. DocConfirmDialog is the shared ModalShell
 * chrome it is built on; the header's Delete confirm uses it too.
 */

import { useState, type ReactNode } from 'react';
import { setDocMode } from '../../api/docsApi';
import type { Doc, DocDetail, DocMode } from '../../api/types';
import { ModalShell } from '../ModalShell';
import './DocModeSwitchDialog.css';

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

export interface DocModeSwitchDialogProps {
  isOpen: boolean;
  doc: DocDetail;
  onClose: () => void;
  /** The row the mode endpoint returned (no `content`). */
  onSwitched: (row: Doc) => void;
}

export function DocModeSwitchDialog({ isOpen, doc, onClose, onSwitched }: DocModeSwitchDialogProps) {
  const target: DocMode = doc.mode === 'public' ? 'private' : 'public';
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const hasWriteShares = doc.shares?.some((s) => s.permission === 'write') ?? false;

  const close = () => {
    setError(null);
    onClose();
  };

  const confirm = async () => {
    setBusy(true);
    setError(null);
    try {
      const row = await setDocMode(doc.id, target);
      setBusy(false);
      onSwitched(row);
      onClose();
    } catch (err) {
      // duplicate_title (another doc already has this title in the target
      // mode) and project_doc_mode_inherited carry a readable server message.
      setBusy(false);
      setError(err instanceof Error && err.message ? err.message : 'Failed to switch the doc mode.');
    }
  };

  return (
    <DocConfirmDialog
      isOpen={isOpen}
      title={`Make '${doc.title}' ${target}?`}
      confirmLabel={target === 'public' ? 'Switch to public' : 'Switch to private'}
      busyLabel="Switching..."
      tone={target === 'public' ? 'warning' : 'default'}
      busy={busy}
      error={error}
      onConfirm={() => void confirm()}
      onClose={close}
    >
      {target === 'public' ? (
        <>
          <p>
            Public conversations run in an internet-enabled sandbox and may send this doc's content to
            third-party sites. Public conversations will be able to read and change it, and private
            conversations will no longer be able to change it.
          </p>
          {hasWriteShares && (
            <p>Write access for people you shared it with will no longer require your approval.</p>
          )}
        </>
      ) : (
        <p>
          Public conversations will no longer see this doc, and private conversations will be able to
          change it again.
        </p>
      )}
    </DocConfirmDialog>
  );
}
