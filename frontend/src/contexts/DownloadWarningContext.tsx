/**
 * The hidden-data acknowledgement every workspace download goes through.
 *
 * Any component can ask `confirmDownload(target)` before handing a workspace
 * file to the browser; the provider renders ONE warning dialog for the whole
 * app and resolves the promise with the user's decision. File types whose
 * content is fully visible as text resolve 'original' at once (see
 * utils/downloadWarnings.ts for the rule); everything else waits for the
 * dialog. There is no remember-me: the warning shows on every download, by
 * design -- a prompt-injected agent can plant a file at any point of a
 * conversation.
 *
 * For a file the server can sanitize (raster images, `warning.sanitizer`)
 * the dialog leads with "Download Sanitized Copy" and offers the original
 * only through a second dialog whose confirm stays disabled until the user
 * ticks an "I know what I am doing" checkbox; Cancel there returns to the
 * first dialog, so the sanitized copy is still one click away.
 */

import React, { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react';
import { DocConfirmDialog } from '../components/docs/DocConfirmDialog';
import {
  getDownloadWarning,
  SANITIZED_IMAGE_EXPLANATION,
  type DownloadTarget,
  type DownloadWarning,
} from '../utils/downloadWarnings';
import './DownloadWarningContext.css';

/**
 * 'cancel': do nothing. 'original': fetch the file as stored.
 * 'sanitized': fetch the server's metadata-stripped copy instead (only ever
 * returned for a target whose warning carries a `sanitizer`).
 */
export type DownloadDecision = 'cancel' | 'original' | 'sanitized';

export interface DownloadWarningContextValue {
  /**
   * Resolves with the user's decision: 'original' at once when no warning
   * applies, otherwise whatever the dialog settled on.
   */
  confirmDownload: (target: DownloadTarget) => Promise<DownloadDecision>;
}

const DownloadWarningContext = createContext<DownloadWarningContextValue | null>(null);

interface PendingWarning {
  target: DownloadTarget;
  warning: DownloadWarning;
  resolve: (decision: DownloadDecision) => void;
}

const ORIGINAL_ACK_LABEL =
  'I understand this file was not inspected, may carry hidden data, and I know what I am doing.';

export function DownloadWarningProvider({ children }: { children: React.ReactNode }) {
  const [pending, setPending] = useState<PendingWarning | null>(null);
  // Second stage of a sanitizable download: the original-file confirm with
  // its checkbox. Reset whenever the pending request changes.
  const [confirmingOriginal, setConfirmingOriginal] = useState(false);
  const [originalAcknowledged, setOriginalAcknowledged] = useState(false);
  // The resolver of the open dialog, reachable from the next request without
  // a stale-closure dance: a second request while one is open cancels the
  // first (its download never started) and takes the dialog over.
  const pendingRef = useRef<PendingWarning | null>(null);

  const settle = useCallback((decision: DownloadDecision) => {
    const current = pendingRef.current;
    pendingRef.current = null;
    setPending(null);
    setConfirmingOriginal(false);
    setOriginalAcknowledged(false);
    current?.resolve(decision);
  }, []);

  const confirmDownload = useCallback((target: DownloadTarget): Promise<DownloadDecision> => {
    const warning = getDownloadWarning(target);
    if (!warning) return Promise.resolve('original');
    return new Promise<DownloadDecision>((resolve) => {
      pendingRef.current?.resolve('cancel');
      const next: PendingWarning = { target, warning, resolve };
      pendingRef.current = next;
      setPending(next);
      setConfirmingOriginal(false);
      setOriginalAcknowledged(false);
    });
  }, []);

  const backToWarning = useCallback(() => {
    setConfirmingOriginal(false);
    setOriginalAcknowledged(false);
  }, []);

  // The warning usually opens on top of another ModalShell (the file viewer,
  // an approval card's preview). Each shell closes on a document-level
  // bubble-phase Escape listener, so an unguarded Escape would dismiss the
  // warning AND the viewer under it. Claim Escape in the capture phase while
  // the warning is open: stopPropagation there keeps every bubble listener
  // from running (see ModalShell), and the warning alone is cancelled -- or,
  // on the original-file confirm, that stage alone is closed.
  useEffect(() => {
    if (pending === null) return;
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return;
      e.stopPropagation();
      if (confirmingOriginal) backToWarning();
      else settle('cancel');
    };
    document.addEventListener('keydown', handleKeyDown, true);
    return () => document.removeEventListener('keydown', handleKeyDown, true);
  }, [pending, confirmingOriginal, backToWarning, settle]);

  const value = useMemo<DownloadWarningContextValue>(() => ({ confirmDownload }), [confirmDownload]);

  const sanitizable = pending?.warning.sanitizer !== undefined;
  const severe = pending?.warning.severity === 'severe';
  const warningTitle = severe
    ? 'This download may be harmful to run'
    : sanitizable
      ? 'This image may carry hidden data'
      : 'This download may carry hidden data';

  return (
    <DownloadWarningContext.Provider value={value}>
      {children}
      <DocConfirmDialog
        isOpen={pending !== null && !confirmingOriginal}
        title={warningTitle}
        confirmLabel={sanitizable ? 'Download Sanitized Copy' : 'Acknowledge and Download'}
        secondaryLabel={sanitizable ? 'Download Original…' : undefined}
        onSecondary={() => setConfirmingOriginal(true)}
        tone={severe ? 'danger' : 'default'}
        busy={false}
        error={null}
        onConfirm={() => settle(sanitizable ? 'sanitized' : 'original')}
        onClose={() => settle('cancel')}
      >
        {pending && (
          <>
            <p>
              <code className="download-warning-name">{pending.target.name}</code>
              {pending.target.kind === 'folder' ? ' will be downloaded as a zip archive.' : ''}
            </p>
            <p>{pending.warning.detail}</p>
            {severe && (
              <p className="download-warning-severe" role="alert">
                Do not run, execute, import, compile or install this file in any capacity -- including
                opening it in a tool that evaluates it automatically -- unless you have reviewed every line
                of it and understand exactly what it does.
              </p>
            )}
            {sanitizable && <p className="download-warning-sanitized">{SANITIZED_IMAGE_EXPLANATION}</p>}
            <p>
              Files in this workspace may have been written by the agent, and a manipulated agent can
              hide sensitive information in them in ways that are hard to detect.
              {sanitizable
                ? ' The sanitized copy is the safe choice; only take the original if you trust how this file was produced.'
                : ' Only continue if you trust how this file was produced.'}
            </p>
          </>
        )}
      </DocConfirmDialog>
      <DocConfirmDialog
        isOpen={pending !== null && confirmingOriginal}
        title="Download the original file?"
        confirmLabel="Download Original"
        tone="danger"
        busy={false}
        error={null}
        confirmDisabled={!originalAcknowledged}
        onConfirm={() => settle('original')}
        onClose={backToWarning}
      >
        {pending && (
          <>
            <p>
              <code className="download-warning-name">{pending.target.name}</code>
              {' will be handed to you byte for byte, including every piece of metadata and any hidden'}
              {' content a manipulated agent may have put there. Nothing here has inspected it.'}
            </p>
            <label className="download-warning-ack">
              <input
                type="checkbox"
                checked={originalAcknowledged}
                onChange={(e) => setOriginalAcknowledged(e.target.checked)}
              />
              <span>{ORIGINAL_ACK_LABEL}</span>
            </label>
          </>
        )}
      </DocConfirmDialog>
    </DownloadWarningContext.Provider>
  );
}

export function useDownloadWarning(): DownloadWarningContextValue {
  const context = useContext(DownloadWarningContext);
  if (!context) {
    throw new Error('useDownloadWarning must be used within a DownloadWarningProvider');
  }
  return context;
}
