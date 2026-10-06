/**
 * Quest Docs viewer (/docs/<id>): a view-only page for one doc.
 *
 * DocHeader (title menu, mode badge, project chip, Show source toggle) over
 * the body -- the markdown rendered through the chat renderer's shared
 * `markdownComponents`, with `assets/<name>` images resolved against the
 * doc's asset route via MarkdownWorkspaceContext.assetBase, or the raw
 * markdown in a <pre> -- and a footer naming the last writer and the update
 * time. Data comes from useDoc, which follows the realtime doc events.
 *
 * Beside the column, an aside holds the doc's Assets list (DocAssetsPanel,
 * from the detail's `assets`). DocViewer.css lays it out: a 280px right
 * gutter card on wide desktops (the chat RightPanel's footprint), below the
 * body at <= 1024px, and on phones (useIsMobile, <= 768px) a collapsed
 * "Assets (N)" section so the document stays first.
 *
 * The body area has three modes, chosen from the header menu and kept per
 * doc (opening another doc starts in `view`): `view` (the rendered doc or
 * its source), `edit` (DocEditor, for owners and write-share recipients)
 * and `history` (DocHistory). The Share dialog (DocShareDialog, owner only)
 * opens over any mode. Save and restore responses carry the new body and
 * are merged straight into the loaded doc (useDoc.applyContent); a save
 * that hit a 409 asks for a re-fetch (`onStale`). Leaving the editor or
 * History puts focus back on the header's title button.
 *
 * Unsaved editor drafts (utils/docDraftBackup.ts, per user in localStorage)
 * surface here too: in view mode a draft that differs from the doc shows
 * "You have unsaved changes from <time>. [Resume editing]" (the editor then
 * offers the restore), or -- without edit access any more -- the read-only
 * DocDraftRecovery panel (Copy text / Download .md / Discard); a doc that
 * vanished while it was edited (404) shows that panel under "This doc no
 * longer exists.". The owner's own Delete drops the draft.
 */

import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import remarkBreaks from 'remark-breaks';
import remarkMath from 'remark-math';
import rehypeHighlight from 'rehype-highlight';
import rehypeKatex from 'rehype-katex';
import { docAssetBase } from '../../api/docsApi';
import type { Doc, DocContentResponse, DocDetail, DocUserRef } from '../../api/types';
import { useAuth } from '../../contexts/AuthContext';
import { useDoc } from '../../hooks/useDoc';
import { useIsMobile } from '../../hooks/useIsMobile';
import {
  DOC_DRAFT_KEY_PREFIX,
  docDraftScope,
  pruneDocDrafts,
  readDocDraft,
  removeDocDraft,
  type DocDraftBackup,
} from '../../utils/docDraftBackup';
import { docsListPath, docViewerPath } from '../../utils/docsRoute';
import { formatRelativeTimestamp, formatTimestamp, parseUTCTimestamp } from '../../utils/formatters';
import remarkMathCurrencyGuard from '../../utils/remarkMathCurrencyGuard';
import { MarkdownWorkspaceContext, markdownComponents } from '../Message';
import { DocAssetsPanel } from './DocAssetsPanel';
import { DocDraftRecovery } from './DocDraftRecovery';
import { DocEditor } from './DocEditor';
import { DocHeader, type DocViewerMode } from './DocHeader';
import { DocHistory } from './DocHistory';
import { DocShareDialog } from './DocShareDialog';
import './DocViewer.css';

const CONVERSATION_SOURCE_PREFIX = 'conversation:';
const ACTION_REQUEST_SOURCE_RE = /^action_request:(.+)$/;
const UI_SOURCE_RE = /^ui:.+$/;

function personName(user: DocUserRef): string {
  return user.name || user.email || 'a deleted user';
}

/**
 * "Last written by" subject for `last_write_source` (owner only; null for
 * share recipients), or null to leave the phrase out (no source or an
 * unknown one):
 * - `ui` (legacy, the owner) -> "you"; `ui:<user id>` -> "you" for the
 *   viewer, else that person (`last_write_user`), "a deleted user" if gone;
 * - `action_request:<id>` -> "<name> (approved change)" when a share
 *   recipient's card made it (`last_write_user`), else "action request #id";
 * - `conversation:<id>` -> a link to the conversation (title and project
 *   from `last_write_conversation`), "a deleted conversation" when gone.
 */
function lastWriter(doc: DocDetail, viewerEmail: string | null): ReactNode {
  const source = doc.last_write_source;
  const user = doc.last_write_user;
  if (source === 'ui') return 'you';
  if (source && UI_SOURCE_RE.test(source)) {
    if (!user) return 'a deleted user';
    const isViewer =
      !!viewerEmail && !!user.email && user.email.toLowerCase() === viewerEmail.toLowerCase();
    return isViewer ? 'you' : personName(user);
  }
  const actionRequest = source ? ACTION_REQUEST_SOURCE_RE.exec(source) : null;
  if (actionRequest) {
    return user ? `${personName(user)} (approved change)` : `action request #${actionRequest[1]}`;
  }
  if (!source?.startsWith(CONVERSATION_SOURCE_PREFIX)) return null;
  const writer = doc.last_write_conversation;
  if (!writer) return 'a deleted conversation';
  const id = encodeURIComponent(writer.id);
  const path = writer.project_id
    ? `/projects/${encodeURIComponent(writer.project_id)}/${id}`
    : `/chats/${id}`;
  return (
    <Link to={path} className="doc-viewer-footer-link">
      {writer.title || 'a conversation'}
    </Link>
  );
}

/**
 * The empty-doc line: "Ask Quest" only when the viewer's conversations can
 * reach the doc (the owner, or a USER doc shared with them -- a shared
 * project doc stays hidden outside its project), the Edit hint with edit
 * access.
 */
function emptyDocText(doc: DocDetail): string {
  const askQuest = !doc.shared_with_me || doc.scope === 'user';
  const canEdit = doc.access.can_edit;
  if (askQuest && canEdit) return 'This doc is empty. Ask Quest to add to it, or choose Edit from the title menu.';
  if (askQuest) return 'This doc is empty. Ask Quest to add to it.';
  if (canEdit) return 'This doc is empty. Choose Edit from the title menu to write it.';
  return 'This doc is empty.';
}

function DocFooter({ doc, viewerEmail }: { doc: DocDetail; viewerEmail: string | null }) {
  const writer = lastWriter(doc, viewerEmail);
  const updated = parseUTCTimestamp(doc.updated_at);
  return (
    <footer className="doc-viewer-footer">
      {writer !== null && (
        <>
          <span className="doc-viewer-footer-writer">Last written by {writer}</span>
          <span className="doc-viewer-footer-sep" aria-hidden="true"> · </span>
        </>
      )}
      <span className="doc-viewer-footer-updated" title={updated.toLocaleString()}>
        Updated {formatRelativeTimestamp(doc.updated_at)}
      </span>
    </footer>
  );
}

export function DocViewer({ docId }: { docId: string }) {
  const { doc, loading, error, notFound, refresh, applyRow, applyContent } = useDoc(docId);
  const navigate = useNavigate();
  const isMobile = useIsMobile();
  const { userEmail } = useAuth();
  const draftScope = docDraftScope(userEmail);
  const viewerRef = useRef<HTMLDivElement>(null);

  // Show source is per doc: tagged with the doc it was turned on for, so
  // opening another doc starts rendered again.
  const [sourceDocId, setSourceDocId] = useState<string | null>(null);
  const showSource = sourceDocId === docId;
  const toggleSource = useCallback(() => {
    setSourceDocId((current) => (current === docId ? null : docId));
  }, [docId]);

  // Body mode and the Share dialog are per doc too, for the same reason.
  const [modeState, setModeState] = useState<{ docId: string; mode: DocViewerMode } | null>(null);
  const mode: DocViewerMode = modeState?.docId === docId ? modeState.mode : 'view';
  const setMode = useCallback(
    (next: DocViewerMode) => setModeState({ docId, mode: next }),
    [docId],
  );
  const [shareDocId, setShareDocId] = useState<string | null>(null);
  const shareOpen = shareDocId === docId;

  const handleSaved = useCallback(
    (row: DocContentResponse) => {
      applyContent(row);
      setMode('view');
    },
    [applyContent, setMode],
  );
  const handleCopied = useCallback(
    (row: Doc) => {
      navigate(docViewerPath(row.id));
    },
    [navigate],
  );
  const leaveToView = useCallback(() => setMode('view'), [setMode]);
  const handleStale = useCallback(() => {
    void refresh();
  }, [refresh]);

  // Back from the editor / History: focus returns to the title button (the
  // control that opened them), not to the top of the page.
  const prevModeRef = useRef(mode);
  useEffect(() => {
    const prev = prevModeRef.current;
    prevModeRef.current = mode;
    if (mode === 'view' && prev !== 'view') {
      viewerRef.current?.querySelector<HTMLElement>('.doc-header-title-button')?.focus();
    }
  }, [mode]);

  // The stored draft of this doc, re-read whenever the editor is not open
  // (it owns the key meanwhile) -- after it closes or unmounts, which runs
  // its final backup flush first -- and when another tab changes drafts.
  useEffect(() => {
    pruneDocDrafts();
  }, []);
  const [storedDraft, setStoredDraft] = useState<{ docId: string; draft: DocDraftBackup | null } | null>(null);
  const rereadDraft = useCallback(() => {
    setStoredDraft({ docId, draft: readDocDraft(draftScope, docId) });
  }, [docId, draftScope]);
  // The editor is mounted only while a doc is shown (not during a load or
  // after a 404, whatever the mode says).
  const editorOpen = mode === 'edit' && doc !== null;
  useEffect(() => {
    if (editorOpen) return;
    rereadDraft();
    const onStorage = (event: StorageEvent) => {
      if (event.key === null || event.key.startsWith(DOC_DRAFT_KEY_PREFIX)) rereadDraft();
    };
    window.addEventListener('storage', onStorage);
    return () => window.removeEventListener('storage', onStorage);
  }, [editorOpen, notFound, rereadDraft]);
  const savedDraft = storedDraft?.docId === docId ? storedDraft.draft : null;
  const discardSavedDraft = useCallback(() => {
    removeDocDraft(draftScope, docId);
    rereadDraft();
  }, [draftScope, docId, rereadDraft]);

  const [retrying, setRetrying] = useState(false);
  const retry = useCallback(async () => {
    setRetrying(true);
    try {
      await refresh();
    } finally {
      setRetrying(false);
    }
  }, [refresh]);

  const markdownCtx = useMemo(() => ({ assetBase: docAssetBase(docId) }), [docId]);

  const projectId = doc?.project_id ?? null;
  const handleDeleted = useCallback(() => {
    // The owner deleted it on purpose (after the confirm): no orphaned draft.
    removeDocDraft(draftScope, docId);
    navigate(docsListPath(projectId));
  }, [navigate, projectId, draftScope, docId]);

  if (loading) {
    return <div className="doc-viewer-state">Loading...</div>;
  }

  if (notFound) {
    return (
      <div className="doc-viewer-state">
        <p className="doc-viewer-state-text">This doc no longer exists.</p>
        {savedDraft && (
          <DocDraftRecovery
            draft={savedDraft}
            message={`Your unsaved draft from ${formatTimestamp(savedDraft.saved_at)} is still in this browser.`}
            onDiscard={discardSavedDraft}
          />
        )}
        <Link to={docsListPath()} className="doc-viewer-state-link">
          All docs
        </Link>
      </div>
    );
  }

  if (!doc) {
    return (
      <div className="doc-viewer-state">
        <p className="doc-viewer-state-text">{error?.message || 'Failed to load the doc.'}</p>
        <button
          type="button"
          className="doc-viewer-retry"
          onClick={() => void retry()}
          disabled={retrying}
        >
          {retrying ? 'Retrying...' : 'Retry'}
        </button>
      </div>
    );
  }

  const isEmpty = doc.content.trim() === '';
  const draftNotice =
    mode === 'view' && savedDraft && savedDraft.content !== doc.content ? savedDraft : null;

  let body: ReactNode;
  if (mode === 'edit') {
    body = (
      <DocEditor
        // Not plain doc.id: DocHeader is a sibling keyed by it.
        key={`edit:${doc.id}`}
        doc={doc}
        onSaved={handleSaved}
        onCancel={leaveToView}
        onStale={handleStale}
      />
    );
  } else if (mode === 'history') {
    body = (
      <DocHistory
        key={`history:${doc.id}`}
        doc={doc}
        onClose={leaveToView}
        onRestored={handleSaved}
        onCopied={handleCopied}
      />
    );
  }

  return (
    <div ref={viewerRef} className={`doc-viewer doc-viewer--${mode}`}>
      <div className="doc-viewer-column">
        <DocHeader
          key={doc.id}
          doc={doc}
          mode={mode}
          showSource={showSource}
          onToggleSource={toggleSource}
          onRowApplied={applyRow}
          onDeleted={handleDeleted}
          onEdit={() => setMode('edit')}
          onShowHistory={() => setMode('history')}
          onShare={() => setShareDocId(docId)}
        />
        {draftNotice && doc.access.can_edit && (
          <div className="doc-viewer-draft-notice" role="status">
            <span>You have unsaved changes from {formatTimestamp(draftNotice.saved_at)}.</span>
            <button
              type="button"
              className="doc-viewer-draft-resume"
              onClick={() => setMode('edit')}
            >
              Resume editing
            </button>
          </div>
        )}
        {draftNotice && !doc.access.can_edit && (
          <div className="doc-viewer-draft-recovery">
            <DocDraftRecovery
              draft={draftNotice}
              message={`You no longer have edit access to this doc. Your unsaved draft from ${formatTimestamp(draftNotice.saved_at)} is still in this browser.`}
              onDiscard={discardSavedDraft}
            />
          </div>
        )}
        {body ?? (
        <div className="doc-viewer-body">
          {isEmpty ? (
            <p className="doc-viewer-empty">{emptyDocText(doc)}</p>
          ) : showSource ? (
            <pre className="doc-viewer-source">{doc.content}</pre>
          ) : (
            // `message-content` = the chat markdown typography; DocViewer.css
            // strips the bubble chrome so it reads like an assistant reply.
            <div className="message-content doc-viewer-markdown">
              <MarkdownWorkspaceContext.Provider value={markdownCtx}>
                <ReactMarkdown
                  remarkPlugins={[remarkGfm, remarkBreaks, remarkMath, remarkMathCurrencyGuard]}
                  rehypePlugins={[rehypeHighlight, rehypeKatex]}
                  components={markdownComponents}
                >
                  {doc.content}
                </ReactMarkdown>
              </MarkdownWorkspaceContext.Provider>
            </div>
          )}
        </div>
        )}
      </div>
      {doc.access.can_share && (
        <DocShareDialog
          key={`share:${doc.id}`}
          doc={doc}
          isOpen={shareOpen}
          onClose={() => setShareDocId(null)}
          onRowApplied={applyRow}
        />
      )}
      <aside className="doc-viewer-aside">
        <DocAssetsPanel
          key={doc.id}
          docId={doc.id}
          assets={doc.assets}
          variant={isMobile ? 'details' : 'card'}
          // Deleting is the owner's, and a view-mode action (the editor
          // could still be about to reference the image).
          canDelete={doc.access.can_delete_assets && mode === 'view'}
          onDeleted={() => void refresh()}
        />
      </aside>
      <DocFooter doc={doc} viewerEmail={userEmail} />
    </div>
  );
}
