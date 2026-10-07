/**
 * The hidden-data acknowledgement every workspace download goes through.
 *
 * Any component can ask `confirmDownload(target)` before handing a workspace
 * file to the browser; the provider renders ONE warning dialog for the whole
 * app and resolves the promise with the user's answer. File types whose
 * content is fully visible as text resolve true at once (see
 * utils/downloadWarnings.ts for the rule); everything else waits for the
 * "Acknowledge and Download" click. There is no remember-me: the warning
 * shows on every download, by design -- a prompt-injected agent can plant a
 * file at any point of a conversation.
 */

import React, { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react';
import { DocConfirmDialog } from '../components/docs/DocConfirmDialog';
import { getDownloadWarning, type DownloadTarget, type DownloadWarning } from '../utils/downloadWarnings';
import './DownloadWarningContext.css';

export interface DownloadWarningContextValue {
  /**
   * Resolves true when the download may proceed (no warning applies, or the
   * user acknowledged it) and false when the user cancelled.
   */
  confirmDownload: (target: DownloadTarget) => Promise<boolean>;
}

const DownloadWarningContext = createContext<DownloadWarningContextValue | null>(null);

interface PendingWarning {
  target: DownloadTarget;
  warning: DownloadWarning;
  resolve: (acknowledged: boolean) => void;
}

export function DownloadWarningProvider({ children }: { children: React.ReactNode }) {
  const [pending, setPending] = useState<PendingWarning | null>(null);
  // The resolver of the open dialog, reachable from the next request without
  // a stale-closure dance: a second request while one is open cancels the
  // first (its download never started) and takes the dialog over.
  const pendingRef = useRef<PendingWarning | null>(null);

  const settle = useCallback((acknowledged: boolean) => {
    const current = pendingRef.current;
    pendingRef.current = null;
    setPending(null);
    current?.resolve(acknowledged);
  }, []);

  const confirmDownload = useCallback((target: DownloadTarget): Promise<boolean> => {
    const warning = getDownloadWarning(target);
    if (!warning) return Promise.resolve(true);
    return new Promise<boolean>((resolve) => {
      pendingRef.current?.resolve(false);
      const next: PendingWarning = { target, warning, resolve };
      pendingRef.current = next;
      setPending(next);
    });
  }, []);

  // The warning usually opens on top of another ModalShell (the file viewer,
  // an approval card's preview). Each shell closes on a document-level
  // bubble-phase Escape listener, so an unguarded Escape would dismiss the
  // warning AND the viewer under it. Claim Escape in the capture phase while
  // the warning is open: stopPropagation there keeps every bubble listener
  // from running (see ModalShell), and the warning alone is cancelled.
  useEffect(() => {
    if (pending === null) return;
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return;
      e.stopPropagation();
      settle(false);
    };
    document.addEventListener('keydown', handleKeyDown, true);
    return () => document.removeEventListener('keydown', handleKeyDown, true);
  }, [pending, settle]);

  const value = useMemo<DownloadWarningContextValue>(() => ({ confirmDownload }), [confirmDownload]);

  return (
    <DownloadWarningContext.Provider value={value}>
      {children}
      <DocConfirmDialog
        isOpen={pending !== null}
        title="This download may carry hidden data"
        confirmLabel="Acknowledge and Download"
        busy={false}
        error={null}
        onConfirm={() => settle(true)}
        onClose={() => settle(false)}
      >
        {pending && (
          <>
            <p>
              <code className="download-warning-name">{pending.target.name}</code>
              {pending.target.kind === 'folder' ? ' will be downloaded as a zip archive.' : ''}
            </p>
            <p>{pending.warning.detail}</p>
            <p>
              Files in this workspace may have been written by the agent, and a manipulated agent can
              hide sensitive information in them in ways that are hard to detect. Only continue if you
              trust how this file was produced.
            </p>
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
