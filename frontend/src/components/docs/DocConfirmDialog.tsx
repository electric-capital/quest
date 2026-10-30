/**
 * Confirm dialog: ModalShell chrome with a title, body paragraphs, an
 * inline error and Cancel / confirm buttons. Shared by the confirms of the
 * doc viewer (header, editor, draft recovery, assets, History, Share
 * dialog, Notes panel), Quest Tables (header, History, schema editor), the
 * chat's right panel and the download warning, so everything below
 * applies to all of them.
 *
 * Accessibility: the title names the dialog (`aria-labelledby`); on open,
 * focus moves to Cancel -- the safe default for a destructive confirm --
 * unless something inside the dialog already took it (a child input that
 * autofocuses runs its effect first); on close, focus returns to whatever
 * had it before the dialog opened, when that element is still on the page.
 * While open, focus is trapped: Tab from the last enabled control wraps to
 * the first and Shift+Tab from the first to the last (`aria-modal` alone
 * does not stop Tab reaching the page behind). From anywhere else (focus
 * dropped to <body>, the page beneath, a control inside that is not a tab
 * stop) Tab enters at the first control and Shift+Tab at the last; focus in
 * another modal opened on top of this one (later in the document, as
 * portals are appended) is left to that modal. A browser drops focus from
 * a button as it is disabled, so when a confirm fails (busy ends with the
 * dialog still open) and focus was lost, it goes back to the confirm
 * button, or Cancel while that stays disabled; while there is an error the
 * confirm button is described by it.
 */

import { useEffect, useId, useRef, type ReactNode } from 'react';
import { ModalShell } from '../ModalShell';
import './DocConfirmDialog.css';

const FOCUSABLE = 'a[href], button, input, select, textarea, summary, [tabindex]';

/** The dialog's tab stops in order: enabled and not taken out with tabindex -1. */
function tabStops(root: HTMLElement): HTMLElement[] {
  return [...root.querySelectorAll<HTMLElement>(FOCUSABLE)].filter(
    (el) =>
      !el.matches(':disabled') &&
      el.getAttribute('tabindex') !== '-1' &&
      !(el instanceof HTMLInputElement && el.type === 'hidden'),
  );
}

function isFocusLost(): boolean {
  const active = document.activeElement;
  return !active || active === document.body || !active.isConnected;
}

/** `active` is in another modal dialog opened on top of `modal` (portaled after it). */
function isInModalAbove(modal: HTMLElement, active: Element | null): boolean {
  const own = modal.closest('[aria-modal="true"]');
  const other = active?.closest('[aria-modal="true"]');
  if (!own || !other || other === own) return false;
  return Boolean(own.compareDocumentPosition(other) & Node.DOCUMENT_POSITION_FOLLOWING);
}

export type DocConfirmTone = 'default' | 'danger';

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
  /** Keeps the confirm button disabled until a condition in the body is met. */
  confirmDisabled?: boolean;
  /**
   * An optional third, neutral action between Cancel and confirm (the
   * download warning's "Download Original..."). Rendered only with a label.
   */
  secondaryLabel?: string;
  onSecondary?: () => void;
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
  confirmDisabled = false,
  secondaryLabel,
  onSecondary,
  onConfirm,
  onClose,
}: DocConfirmDialogProps) {
  const titleId = useId();
  const errorId = useId();
  const modalRef = useRef<HTMLDivElement>(null);
  const cancelRef = useRef<HTMLButtonElement>(null);
  const confirmRef = useRef<HTMLButtonElement>(null);
  const wasBusyRef = useRef(busy);

  // Initial focus on open, focus return on close.
  useEffect(() => {
    if (!isOpen) return;
    const active = document.activeElement;
    // A child that autofocused ran its effect first: focus is already in
    // here, the opener is unknown, and the child owns focus.
    const focusedInside = Boolean(active && modalRef.current?.contains(active));
    const opener = !focusedInside && active instanceof HTMLElement ? active : null;
    if (!focusedInside) cancelRef.current?.focus();
    return () => {
      if (opener && opener.isConnected && opener !== document.body) {
        opener.focus();
      }
    };
  }, [isOpen]);

  // The focus trap: Tab and Shift+Tab wrap at the ends and re-enter from
  // anywhere that is not one of the stops; between the stops the browser
  // moves focus as usual.
  useEffect(() => {
    if (!isOpen) return;
    const onKeyDown = (e: KeyboardEvent) => {
      const modal = modalRef.current;
      if (e.key !== 'Tab' || e.defaultPrevented || !modal) return;
      const active = document.activeElement;
      if (isInModalAbove(modal, active)) return;
      const stops = tabStops(modal);
      if (stops.length === 0) {
        e.preventDefault();
        return;
      }
      const index = active instanceof HTMLElement ? stops.indexOf(active) : -1;
      const atEnd = index === (e.shiftKey ? 0 : stops.length - 1);
      if (index === -1 || atEnd) {
        e.preventDefault();
        (e.shiftKey ? stops[stops.length - 1] : stops[0]).focus();
      }
    };
    document.addEventListener('keydown', onKeyDown);
    return () => document.removeEventListener('keydown', onKeyDown);
  }, [isOpen]);

  // A failed confirm leaves the dialog open: focus, dropped from the button
  // disabled while busy, goes back to the confirm button (else Cancel).
  useEffect(() => {
    const wasBusy = wasBusyRef.current;
    wasBusyRef.current = busy;
    if (!isOpen || busy || !wasBusy || !isFocusLost()) return;
    const confirm = confirmRef.current;
    (confirm && !confirm.disabled ? confirm : cancelRef.current)?.focus();
  }, [isOpen, busy]);

  // A request in flight cannot be called back: keep the dialog up until it
  // settles instead of letting Escape / a backdrop click hide it.
  const close = busy ? () => {} : onClose;
  return (
    <ModalShell
      isOpen={isOpen}
      onClose={close}
      overlayClassName="doc-dialog-overlay"
      modalClassName="doc-dialog"
      modalRef={modalRef}
      ariaLabelledBy={titleId}
    >
      <h2 id={titleId} className="doc-dialog-title">{title}</h2>
      <div className="doc-dialog-body">{children}</div>
      {error && (
        <div id={errorId} className="doc-dialog-error" role="alert">
          {error}
        </div>
      )}
      <div className="doc-dialog-actions">
        <button
          ref={cancelRef}
          type="button"
          className="doc-dialog-button doc-dialog-cancel"
          onClick={close}
          disabled={busy}
        >
          Cancel
        </button>
        {secondaryLabel && (
          <button
            type="button"
            className="doc-dialog-button doc-dialog-secondary"
            onClick={onSecondary}
            disabled={busy}
          >
            {secondaryLabel}
          </button>
        )}
        <button
          ref={confirmRef}
          type="button"
          className={`doc-dialog-button doc-dialog-confirm doc-dialog-confirm--${tone}`}
          aria-describedby={error ? errorId : undefined}
          onClick={onConfirm}
          disabled={busy || confirmDisabled}
        >
          {busy && busyLabel ? busyLabel : confirmLabel}
        </button>
      </div>
    </ModalShell>
  );
}
