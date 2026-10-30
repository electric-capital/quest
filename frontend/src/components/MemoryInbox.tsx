/**
 * The inbox's Memories tab: every suggestion of the user awaiting review in
 * one place.
 *
 * Open suggestions (personal ``suggest_memory``, project
 * ``suggest_project_memory`` and doc note ``suggest_doc_note`` proposals,
 * across all conversations) come first, each with the same editable row as
 * the composer panel plus a line saying what kind it is, which conversation
 * it came from and, for a doc note, which doc it is for. Below them, the
 * recently reviewed (accepted / dismissed) suggestions, newest first, paged
 * in as the user scrolls. The lists come from ``/app/api/memory-inbox``
 * (chat/memory_inbox.py); accept and dismiss go through each kind's own
 * endpoints (``KIND_API``): api/client.ts for the two memory kinds, the doc
 * note suggestion endpoints of api/docsApi.ts for doc rows. A doc
 * instructions accept sends the version its diff was rendered against
 * (docNoteReview.ts); when the instructions changed meanwhile, the 409
 * ``stale_instructions`` re-bases the row on the current text it carries
 * (re-reading the open list when it carries none), keeps the draft and
 * shows the server's message, as the composer bubble does.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import type { MouseEvent } from 'react';
import { Link } from 'react-router-dom';
import { Brain, FileText, FolderBookmark, LoaderCircle, Moon } from 'lucide-react';
import type { LucideIcon } from 'lucide-react';
import {
  acceptMemorySuggestion,
  acceptProjectMemorySuggestion,
  dismissMemorySuggestion,
  dismissProjectMemorySuggestion,
  fetchMemoryInboxPending,
  fetchMemoryInboxResolved,
} from '../api/client';
import {
  acceptDocNoteSuggestion,
  dismissDocNoteSuggestion,
  isStaleInstructionsError,
  staleInstructionsCurrent,
} from '../api/docsApi';
import { ApiClientError } from '../api/request';
import type {
  DocNoteInboxSuggestion,
  MemoryInboxRow,
  MemoryInboxSuggestion,
  MemorySuggestion,
} from '../api/types';
import { persistentWebSocket } from '../services/PersistentWebSocket';
import { docViewerPath } from '../utils/docsRoute';
import { formatRelativeTimestamp } from '../utils/formatters';
import { MemorySuggestionRow } from './MemorySuggestionRow';
import type { MemorySuggestionRowStateProps } from './MemorySuggestionRow';
import { canAcceptDocNote, docNoteAcceptOptions, withCurrentInstructions } from './docNoteReview';
import './MemorySuggestions.css';
import './MemoryInbox.css';

/** Rows per page of the reviewed list. */
export const RESOLVED_PAGE_SIZE = 20;

/** What an accept or dismiss response says about the row it resolved. */
type ResolvedFields = Pick<MemorySuggestion, 'status' | 'resolved_at' | 'memory_id'>;

/** One kind's endpoints and wording, for its rows of type ``R``. */
interface KindApi<R extends MemoryInboxRow> {
  /** Accept with the row's draft (undefined = the proposed text, never edited). */
  accept: (row: R, draft: string | undefined) => Promise<{ suggestion: ResolvedFields }>;
  dismiss: (suggestionId: string) => Promise<{ suggestion: ResolvedFields }>;
  saveError: string;
  /** Accept all skips a row this refuses (absent = every row is tried). */
  canAccept?: (row: R, draft: string | undefined) => boolean;
}

/**
 * The edited text to send, or undefined while the draft keeps the proposal.
 * An archive-only row (no content) has no text to edit, so it never sends one.
 */
function editedText(row: MemoryInboxSuggestion, draft: string | undefined): string | undefined {
  if (row.content === null) return undefined;
  const edited = draft !== undefined && draft.trim() !== row.content.trim();
  return edited ? draft.trim() : undefined;
}

const KIND_API: {
  user: KindApi<MemoryInboxSuggestion>;
  project: KindApi<MemoryInboxSuggestion>;
  doc: KindApi<DocNoteInboxSuggestion>;
} = {
  user: {
    accept: (row, draft) => acceptMemorySuggestion(row.id, editedText(row, draft)),
    dismiss: dismissMemorySuggestion,
    saveError: 'Failed to save memory',
  },
  project: {
    accept: (row, draft) => acceptProjectMemorySuggestion(row.id, editedText(row, draft)),
    dismiss: dismissProjectMemorySuggestion,
    saveError: 'Failed to save project memory',
  },
  doc: {
    accept: (row, draft) => acceptDocNoteSuggestion(row.id, docNoteAcceptOptions(row, draft)),
    dismiss: dismissDocNoteSuggestion,
    saveError: 'Failed to save doc note',
    // A blank draft would clear a doc's instructions; one over the cap is refused.
    canAccept: canAcceptDocNote,
  },
};

/** A row's kind API bound to the row, so callers need not narrow its kind. */
interface RowApi {
  accept: (draft: string | undefined) => Promise<{ suggestion: ResolvedFields }>;
  dismiss: () => Promise<{ suggestion: ResolvedFields }>;
  saveError: string;
  canAccept: (draft: string | undefined) => boolean;
}

function bindRow<R extends MemoryInboxRow>(api: KindApi<R>, row: R): RowApi {
  return {
    accept: (draft) => api.accept(row, draft),
    dismiss: () => api.dismiss(row.id),
    saveError: api.saveError,
    canAccept: (draft) => api.canAccept?.(row, draft) ?? true,
  };
}

function rowApi(row: MemoryInboxRow): RowApi {
  return row.kind === 'doc' ? bindRow(KIND_API.doc, row) : bindRow(KIND_API[row.kind], row);
}

/**
 * A row of a kind ``KIND_API`` has endpoints for. A kind the server adds
 * before this tab knows it is left out of both lists rather than shown with
 * buttons that cannot work.
 */
function isReviewableRow(row: MemoryInboxRow): boolean {
  return Object.hasOwn(KIND_API, row.kind);
}

/** The source line's kind pill. */
const KIND_LABELS: Record<MemoryInboxRow['kind'], { Icon: LucideIcon; label: string; title: string }> = {
  user: {
    Icon: Brain,
    label: 'Memory',
    title: 'A personal memory: kept across all your chats',
  },
  project: {
    Icon: FolderBookmark,
    label: 'Project memory',
    title: 'A project memory: kept for every chat in the project',
  },
  doc: {
    Icon: FileText,
    label: 'Doc note',
    title: 'A doc note: instructions or a memory for one doc',
  },
};

const UNAVAILABLE_DOC_LABEL = 'Unavailable doc';
const UNAVAILABLE_DOC_TITLE = 'This doc was deleted or you no longer have access to it';

interface MemoryInboxProps {
  onGoToConversation: (conversationId: string, projectId?: string | null) => void;
  /** Called on a plain click of a doc row's doc link, before the navigation. */
  onOpenDoc?: () => void;
}

function errorMessage(err: unknown, fallback: string): string {
  if (err instanceof Error && err.message) return err.message;
  return fallback;
}

/**
 * A doc row's doc: its title linking to the doc viewer, or a neutral
 * "Unavailable doc" when the doc is gone or no longer visible to the user.
 */
function DocSource({
  suggestion,
  onOpenDoc,
}: { suggestion: DocNoteInboxSuggestion; onOpenDoc?: () => void }) {
  const docTitle = suggestion.doc_title ?? '';
  if (!docTitle) {
    return (
      <span className="memory-inbox-doc-unavailable" title={UNAVAILABLE_DOC_TITLE}>
        {UNAVAILABLE_DOC_LABEL}
      </span>
    );
  }
  const onClick = (e: MouseEvent<HTMLAnchorElement>) => {
    // A modified click opens a new tab; the inbox stays where it is.
    if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    onOpenDoc?.();
  };
  return (
    <Link
      to={docViewerPath(suggestion.doc_id)}
      className="memory-inbox-doc-link"
      title={docTitle}
      onClick={onClick}
    >
      {docTitle}
    </Link>
  );
}

/**
 * What a suggestion is and where it came from: the kind pill, a doc note's
 * doc, then the chat (and project) it was proposed in, with a link to it,
 * or the memory review (a dream) that proposed it.
 */
function SuggestionSource({
  suggestion,
  onGoToConversation,
  onOpenDoc,
}: {
  suggestion: MemoryInboxRow;
  onGoToConversation: MemoryInboxProps['onGoToConversation'];
  onOpenDoc?: () => void;
}) {
  const { Icon, label, title } = KIND_LABELS[suggestion.kind];
  const conversationId = suggestion.conversation_id;
  // A dream's suggestion has no chat to link to (doc notes always have one).
  const fromReview = suggestion.kind !== 'doc' && suggestion.source === 'dream';
  return (
    <div className="memory-inbox-source">
      <span className={`memory-inbox-kind memory-inbox-kind--${suggestion.kind}`} title={title}>
        <Icon size={12} aria-hidden="true" />
        {label}
      </span>
      {suggestion.kind === 'doc' && <DocSource suggestion={suggestion} onOpenDoc={onOpenDoc} />}
      {fromReview || conversationId === null ? (
        <span
          className="memory-inbox-from memory-inbox-from--dream"
          title="Proposed by the periodic review of your saved memories, not by a chat"
        >
          <Moon size={12} aria-hidden="true" />
          From a memory review
        </span>
      ) : (
        <span className="memory-inbox-from">
          From
          {' '}
          <button
            type="button"
            className="memory-inbox-conversation-link"
            onClick={() => onGoToConversation(conversationId, suggestion.project_id)}
          >
            {suggestion.conversation_title || 'Untitled chat'}
          </button>
          {suggestion.project_name && (
            <>
              {' in '}
              <span className="memory-inbox-project">{suggestion.project_name}</span>
            </>
          )}
        </span>
      )}
    </div>
  );
}

export function MemoryInbox({ onGoToConversation, onOpenDoc }: MemoryInboxProps) {
  // null until the first fetch lands.
  const [pending, setPending] = useState<MemoryInboxRow[] | null>(null);
  const [pendingError, setPendingError] = useState<string | null>(null);
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [busyIds, setBusyIds] = useState<Set<string>>(() => new Set());
  const [rowErrors, setRowErrors] = useState<Record<string, string>>({});
  const [isBulkBusy, setIsBulkBusy] = useState(false);

  const [resolved, setResolved] = useState<MemoryInboxRow[]>([]);
  const [resolvedLoaded, setResolvedLoaded] = useState(false);
  const [resolvedError, setResolvedError] = useState<string | null>(null);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  const loadingMoreRef = useRef(false);
  const sentinelRef = useRef<HTMLDivElement>(null);

  // Bumped on every local accept / dismiss / re-base so a pending-list fetch
  // already in flight cannot resurrect the row just removed (or undo the
  // re-base).
  const generationRef = useRef(0);

  const loadPending = useCallback(async () => {
    const generation = generationRef.current;
    try {
      const resp = await fetchMemoryInboxPending();
      if (generation !== generationRef.current) return;
      const rows = resp.suggestions.filter(isReviewableRow);
      setPending(rows);
      setPendingError(null);
      setDrafts((prev) => {
        const live = new Set(rows.map((s) => s.id));
        const next: Record<string, string> = {};
        for (const [id, text] of Object.entries(prev)) {
          if (live.has(id)) next[id] = text;
        }
        return next;
      });
    } catch (err) {
      if (generation !== generationRef.current) return;
      setPendingError(errorMessage(err, 'Failed to load suggestions'));
      setPending((prev) => prev ?? []);
    }
  }, []);

  const loadMoreResolved = useCallback(async (cursor: string | null, replace: boolean) => {
    if (loadingMoreRef.current) return;
    loadingMoreRef.current = true;
    setLoadingMore(true);
    try {
      const resp = await fetchMemoryInboxResolved(cursor, RESOLVED_PAGE_SIZE);
      const rows = resp.suggestions.filter(isReviewableRow);
      setResolved((prev) => {
        if (replace) return rows;
        // A row accepted here while the page was in flight may come back
        // in it too; keep the first occurrence.
        const seen = new Set(prev.map((s) => s.id));
        return [...prev, ...rows.filter((s) => !seen.has(s.id))];
      });
      setNextCursor(resp.next_cursor);
      setResolvedError(null);
    } catch (err) {
      setResolvedError(errorMessage(err, 'Failed to load reviewed suggestions'));
    } finally {
      loadingMoreRef.current = false;
      setLoadingMore(false);
      setResolvedLoaded(true);
    }
  }, []);

  useEffect(() => {
    void loadPending();
    void loadMoreResolved(null, true);
  }, [loadPending, loadMoreResolved]);

  // A suggestion registered mid-turn or resolved from another tab: the
  // per-user globals carry the user's new totals, so re-read the open list.
  useEffect(() => {
    return persistentWebSocket.onGlobalEvent((event) => {
      if (
        event.type !== 'memory_suggestions_changed'
        && event.type !== 'project_memory_suggestions_changed'
        && event.type !== 'doc_note_suggestions_changed'
      ) return;
      void loadPending();
    });
  }, [loadPending]);

  // Infinite scroll: the sentinel under the reviewed list asks for the next
  // page whenever it scrolls into view (clipped by the inbox's scroll pane,
  // so a long list only loads as the user reaches its end).
  useEffect(() => {
    const sentinel = sentinelRef.current;
    if (!sentinel || !nextCursor || typeof IntersectionObserver === 'undefined') return;
    const observer = new IntersectionObserver((entries) => {
      if (entries.some((entry) => entry.isIntersecting)) {
        void loadMoreResolved(nextCursor, false);
      }
    }, { rootMargin: '200px' });
    observer.observe(sentinel);
    return () => observer.disconnect();
  }, [nextCursor, loadMoreResolved]);

  const setBusy = useCallback((id: string, busy: boolean) => {
    setBusyIds((prev) => {
      const next = new Set(prev);
      if (busy) next.add(id);
      else next.delete(id);
      return next;
    });
  }, []);

  const removePending = useCallback((id: string) => {
    generationRef.current += 1;
    setPending((prev) => (prev ? prev.filter((s) => s.id !== id) : prev));
    setDrafts((prev) => {
      if (!(id in prev)) return prev;
      const next = { ...prev };
      delete next[id];
      return next;
    });
    setRowErrors((prev) => {
      if (!(id in prev)) return prev;
      const next = { ...prev };
      delete next[id];
      return next;
    });
  }, []);

  const replacePending = useCallback((row: MemoryInboxRow) => {
    generationRef.current += 1;
    setPending((prev) => (prev ? prev.map((s) => (s.id === row.id ? row : s)) : prev));
  }, []);

  const resolveOne = useCallback(async (
    suggestion: MemoryInboxRow,
    action: 'accept' | 'dismiss',
  ): Promise<boolean> => {
    const api = rowApi(suggestion);
    const draft = drafts[suggestion.id];
    setBusy(suggestion.id, true);
    setRowErrors((prev) => {
      if (!(suggestion.id in prev)) return prev;
      const next = { ...prev };
      delete next[suggestion.id];
      return next;
    });
    try {
      const { suggestion: outcome } = action === 'accept'
        ? await api.accept(draft)
        : await api.dismiss();
      removePending(suggestion.id);
      // The row moves to the top of the reviewed list right away, showing
      // the text as saved (the edited draft on an accept). The response is
      // the bare suggestion, so the inbox-only fields (and a doc row's doc)
      // stay from the row.
      const fields = {
        status: outcome.status,
        resolved_at: outcome.resolved_at,
        memory_id: outcome.memory_id,
      };
      const saved = action === 'accept' && draft !== undefined ? draft.trim() : undefined;
      // Narrowed per kind: a memory row's content may be null (archive-only).
      const reviewed: MemoryInboxRow = suggestion.kind === 'doc'
        ? { ...suggestion, ...fields, content: saved ?? suggestion.content }
        : { ...suggestion, ...fields, content: saved ?? suggestion.content };
      setResolved((prev) => [reviewed, ...prev.filter((s) => s.id !== suggestion.id)]);
      return true;
    } catch (err) {
      // 409 suggestion_resolved: resolved from another tab -- the row is
      // gone either way; the next reviewed-list load shows the outcome.
      if (err instanceof ApiClientError && err.errorCode === 'suggestion_resolved') {
        removePending(suggestion.id);
        return true;
      }
      // A doc instructions accept against instructions that changed since
      // its diff was rendered: re-base the row on the current text so the
      // diff shows what accepting now replaces (no text in the 409: re-read
      // the list). The draft stays, and the next Accept sends the new version.
      if (action === 'accept' && suggestion.kind === 'doc' && isStaleInstructionsError(err)) {
        const current = staleInstructionsCurrent(err);
        if (current) replacePending({ ...suggestion, ...withCurrentInstructions(suggestion, current) });
        else void loadPending();
      }
      setRowErrors((prev) => ({
        ...prev,
        [suggestion.id]: errorMessage(
          err,
          action === 'accept' ? api.saveError : 'Failed to dismiss',
        ),
      }));
      return false;
    } finally {
      setBusy(suggestion.id, false);
    }
  }, [drafts, removePending, replacePending, loadPending, setBusy]);

  const resolveAll = useCallback(async (action: 'accept' | 'dismiss') => {
    if (isBulkBusy || !pending) return;
    setIsBulkBusy(true);
    try {
      // Sequential so a failure leaves the rest intact for a retry.
      for (const suggestion of pending) {
        if (busyIds.has(suggestion.id)) continue;
        if (action === 'accept' && !rowApi(suggestion).canAccept(drafts[suggestion.id])) continue;
        await resolveOne(suggestion, action);
      }
    } finally {
      setIsBulkBusy(false);
    }
  }, [isBulkBusy, pending, busyIds, drafts, resolveOne]);

  const pendingCount = pending?.length ?? 0;

  return (
    <div className="memory-inbox">
      <section className="memory-inbox-section" aria-labelledby="memory-inbox-open-title">
        <div className="memory-inbox-section-header">
          <h3 id="memory-inbox-open-title" className="memory-inbox-section-title">
            Open suggestions
            <span className="memory-inbox-section-count">{pendingCount}</span>
          </h3>
          {pendingCount > 1 && (
            <div className="memory-inbox-bulk">
              <button
                type="button"
                className="memory-suggestions-bulk"
                onClick={() => resolveAll('accept')}
                disabled={isBulkBusy}
              >
                Accept all
              </button>
              <button
                type="button"
                className="memory-suggestions-bulk memory-suggestions-bulk-dismiss"
                onClick={() => resolveAll('dismiss')}
                disabled={isBulkBusy}
              >
                Dismiss all
              </button>
            </div>
          )}
        </div>
        <p className="memory-inbox-hint">
          Memories and doc notes the assistant proposed in your chats, and
          clean-ups from the periodic review of your saved memories. Edit the
          text if you like, then accept to save it or dismiss it. Nothing
          changes until you accept.
        </p>
        {pendingError && (
          <div className="memory-suggestions-error" role="alert">{pendingError}</div>
        )}
        {pending === null ? (
          <div className="memory-inbox-status">
            <LoaderCircle size={16} className="memory-suggestions-spinner" aria-hidden="true" />
            Loading suggestions...
          </div>
        ) : pending.length === 0 ? (
          <div className="memory-inbox-empty">No open suggestions. You're all caught up.</div>
        ) : (
          <div className="memory-inbox-list">
            {pending.map((suggestion) => {
              const state: Omit<MemorySuggestionRowStateProps, 'children'> = {
                draft: drafts[suggestion.id] ?? suggestion.content ?? '',
                onDraftChange: (text) => setDrafts((prev) => ({ ...prev, [suggestion.id]: text })),
                busy: busyIds.has(suggestion.id),
                error: rowErrors[suggestion.id],
                onAccept: () => resolveOne(suggestion, 'accept'),
                onDismiss: () => resolveOne(suggestion, 'dismiss'),
              };
              const source = (
                <SuggestionSource
                  suggestion={suggestion}
                  onGoToConversation={onGoToConversation}
                  onOpenDoc={onOpenDoc}
                />
              );
              // A doc row's source line names the doc, so the row does not.
              return suggestion.kind === 'doc' ? (
                <MemorySuggestionRow
                  key={suggestion.id}
                  kind="doc"
                  suggestion={suggestion}
                  showDocLink={false}
                  {...state}
                >
                  {source}
                </MemorySuggestionRow>
              ) : (
                <MemorySuggestionRow
                  key={suggestion.id}
                  kind={suggestion.kind}
                  suggestion={suggestion}
                  {...state}
                >
                  {source}
                </MemorySuggestionRow>
              );
            })}
          </div>
        )}
      </section>

      <section className="memory-inbox-section" aria-labelledby="memory-inbox-reviewed-title">
        <div className="memory-inbox-section-header">
          <h3 id="memory-inbox-reviewed-title" className="memory-inbox-section-title">
            Recently reviewed
          </h3>
        </div>
        {resolvedError && (
          <div className="memory-suggestions-error" role="alert">{resolvedError}</div>
        )}
        {!resolvedLoaded ? (
          <div className="memory-inbox-status">
            <LoaderCircle size={16} className="memory-suggestions-spinner" aria-hidden="true" />
            Loading...
          </div>
        ) : resolved.length === 0 ? (
          // A page whose rows were all left out still has a cursor: more may
          // follow, so the list is not known to be empty yet.
          !resolvedError && !nextCursor && (
            <div className="memory-inbox-empty">Nothing reviewed yet.</div>
          )
        ) : (
          <div className="memory-inbox-list">
            {resolved.map((suggestion) => (
              <div
                key={suggestion.id}
                className={`memory-inbox-reviewed memory-inbox-reviewed--${suggestion.status}`}
              >
                <div className="memory-inbox-reviewed-header">
                  <span className={`memory-inbox-status-pill memory-inbox-status-pill--${suggestion.status}`}>
                    {suggestion.status === 'accepted' ? 'Accepted' : 'Dismissed'}
                  </span>
                  {suggestion.resolved_at && (
                    <span className="memory-inbox-time" title={suggestion.resolved_at}>
                      {formatRelativeTimestamp(suggestion.resolved_at)}
                    </span>
                  )}
                </div>
                <div className="memory-inbox-reviewed-content">
                  {suggestion.content ?? (
                    <span className="memory-inbox-reviewed-archive">
                      {suggestion.status === 'accepted'
                        ? 'Archived a memory'
                        : 'Proposed archiving a memory'}
                    </span>
                  )}
                </div>
                <SuggestionSource
                  suggestion={suggestion}
                  onGoToConversation={onGoToConversation}
                  onOpenDoc={onOpenDoc}
                />
              </div>
            ))}
          </div>
        )}
        {/* Sentinel + spinner for the next page; the observer above watches it. */}
        <div ref={sentinelRef} className="memory-inbox-sentinel" aria-hidden="true" />
        {loadingMore && resolvedLoaded && (
          <div className="memory-inbox-status">
            <LoaderCircle size={16} className="memory-suggestions-spinner" aria-hidden="true" />
            Loading more...
          </div>
        )}
        {resolvedLoaded && nextCursor && !loadingMore && (
          <button
            type="button"
            className="memory-inbox-load-more"
            onClick={() => loadMoreResolved(nextCursor, false)}
          >
            Load more
          </button>
        )}
      </section>
    </div>
  );
}
