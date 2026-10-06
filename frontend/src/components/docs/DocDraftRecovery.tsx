/**
 * An unsaved editor draft (utils/docDraftBackup.ts) the viewer can no longer
 * hand back to the editor: the doc was deleted or became invisible (404),
 * or the viewer lost edit access. Shows the draft read-only with Copy text,
 * Download .md (a client-side Blob named after the doc's last known title)
 * and Discard (behind a confirm), so the text is never silently lost.
 */

import { useRef, useState } from 'react';
import { Copy, FileDown, Trash2 } from 'lucide-react';
import { docDraftFileName, type DocDraftBackup } from '../../utils/docDraftBackup';
import { DocConfirmDialog } from './DocConfirmDialog';
import './DocDraftRecovery.css';

export interface DocDraftRecoveryProps {
  draft: DocDraftBackup;
  /** Why the draft is shown here (first line of the panel). */
  message: string;
  /** Remove the draft (after the confirm). */
  onDiscard: () => void;
}

/** Save `text` as a .md file through a temporary object URL. */
function downloadText(text: string, filename: string): void {
  const url = URL.createObjectURL(new Blob([text], { type: 'text/markdown;charset=utf-8' }));
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  link.rel = 'noopener';
  document.body.appendChild(link);
  link.click();
  link.remove();
  // Revoked a moment later: some browsers start the download asynchronously.
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export function DocDraftRecovery({ draft, message, onDiscard }: DocDraftRecoveryProps) {
  const contentRef = useRef<HTMLPreElement>(null);
  const [status, setStatus] = useState('');
  const [confirmOpen, setConfirmOpen] = useState(false);

  const copy = async () => {
    try {
      if (!navigator.clipboard) throw new Error('no clipboard');
      await navigator.clipboard.writeText(draft.content);
      setStatus('Copied to the clipboard.');
    } catch {
      // No clipboard access: select the text for a manual copy.
      const pre = contentRef.current;
      const selection = window.getSelection();
      if (pre && selection) selection.selectAllChildren(pre);
      setStatus("Couldn't copy automatically: the text is selected, copy it with Ctrl+C / Cmd+C.");
    }
  };

  const download = () => {
    try {
      downloadText(draft.content, docDraftFileName(draft.title));
      setStatus('');
    } catch {
      setStatus("Couldn't download the draft in this browser.");
    }
  };

  return (
    <section className="doc-draft-recovery" aria-label="Unsaved draft">
      <p className="doc-draft-recovery-message">{message}</p>
      <div className="doc-draft-recovery-actions">
        <button type="button" className="doc-draft-recovery-button" onClick={() => void copy()}>
          <Copy size={14} aria-hidden="true" />
          <span>Copy text</span>
        </button>
        <button type="button" className="doc-draft-recovery-button" onClick={download}>
          <FileDown size={14} aria-hidden="true" />
          <span>Download .md</span>
        </button>
        <button
          type="button"
          className="doc-draft-recovery-button doc-draft-recovery-button--danger"
          onClick={() => setConfirmOpen(true)}
        >
          <Trash2 size={14} aria-hidden="true" />
          <span>Discard</span>
        </button>
      </div>
      <p className="doc-draft-recovery-status" role="status">
        {status}
      </p>
      <pre ref={contentRef} className="doc-draft-recovery-content" tabIndex={0} aria-label="Draft text">
        {draft.content}
      </pre>
      <DocConfirmDialog
        isOpen={confirmOpen}
        title="Discard your unsaved draft?"
        confirmLabel="Discard draft"
        tone="danger"
        busy={false}
        error={null}
        onConfirm={() => {
          setConfirmOpen(false);
          onDiscard();
        }}
        onClose={() => setConfirmOpen(false)}
      >
        <p>The draft is removed from this browser. This cannot be undone.</p>
      </DocConfirmDialog>
    </section>
  );
}
