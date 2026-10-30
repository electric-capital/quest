/**
 * The inbox's Memories tab: every suggestion of the user awaiting review in
 * one place.
 *
 * Open suggestions (personal ``suggest_memory``, project
 * ``suggest_project_memory`` and doc note ``suggest_doc_note`` proposals
 * across all conversations, and a dream's clean-ups of saved memories)
 * come first, each with the same row as the composer panel plus a line
 * saying what kind it is, for a doc note which doc it is for, and which
 * conversation it came from; a dream's row has no conversation and names
 * the memory review instead. Below them, the recently reviewed (accepted /
 * dismissed) suggestions, newest first, paged in as the user scrolls. The
 * lists come from ``/app/api/memory-inbox`` (chat/memory_inbox.py); accept
 * and dismiss go through each kind's own endpoints (``KIND_API``):
 * api/client.ts for the two memory kinds, the doc note suggestion endpoints
 * of api/docsApi.ts for doc rows. Accept all skips a row its own Accept
 * refuses (the kind's ``canAccept``). A doc instructions accept sends the
 * version its diff was rendered against (docNoteReview.ts); when the
 * instructions changed meanwhile, the 409 ``stale_instructions`` (the doc
 * kind's ``conflict``) re-bases the row on the current text it carries
 * (unless the row in state is already as new), keeps the draft, shows the
 * server's message and re-reads the open list, as the composer bubble does.
 *
 * The open list is re-read on every suggestion event. The newest read to
 * start wins (an older one landing later is dropped), and rows accepted or
 * dismissed here are left out of every read, so a read that started before
 * the accept cannot bring the row back while the one the accept's own event
 * starts (often before the accept's response) still lands, with the other
 * rows as the accept left them.
 *
 * Focus stays in the open list as rows are accepted or dismissed, one at a
 * time or all at once: useSuggestionListFocus over the "Open suggestions"
 * section, with its heading (focusable from script only) as the fallback
 * once no row is left.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
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
import { suggestionCountKind } from '../hooks/usePendingSuggestionCounts';
import { useSuggestionListFocus } from '../hooks/useSuggestionListFocus';
import { persistentWebSocket } from '../services/PersistentWebSocket';
import { formatRelativeTimestamp } from '../utils/formatters';
import { DOC_NOTE_LABELS, DocTitleLink, MemorySuggestionRow } from './MemorySuggestionRow';
import type { MemorySuggestionRowStateProps } from './MemorySuggestionRow';
import {
  canAcceptDocNote,
  canAcceptMemory,
  docNoteAcceptOptions,
  editedText,
  rebaseOnStale,
} from './docNoteReview';
import './MemorySuggestions.css';
import './MemoryInbox.css';

/** Rows per page of the reviewed list. */
export const RESOLVED_PAGE_SIZE = 20;

/** What an accept or dismiss response says about the row it resolved. */
type ResolvedFields = Pick<MemorySuggestion, 'status' | 'resolved_at' | 'memory_id'>;

/**
 * A failed accept that is a conflict (the row stays pending but changed
 * underneath): ``rebase`` maps the row as it is in state when the error
 * lands (null: the error carries no data). The row in state is typed as
 * any row, so ``rebase`` checks its kind.
 */
interface Conflict {
  rebase: ((row: MemoryInboxRow) => MemoryInboxRow) | null;
}

/** One kind's endpoints and wording, for its rows of type ``R``. */
interface KindApi<R extends MemoryInboxRow> {
  /** Accept with the row's draft (undefined = the proposed text, never edited). */
  accept: (row: R, draft: string | undefined) => Promise<{ suggestion: ResolvedFields }>;
  dismiss: (suggestionId: string) => Promise<{ suggestion: ResolvedFields }>;
  saveError: string;
  /** Accept all skips a row this refuses (absent = every row is tried). */
  canAccept?: (row: R, draft: string | undefined) => boolean;
  /**
   * The conflict a failed accept is, or null for an ordinary failure
   * (absent = never a conflict). A conflict always re-reads the open list
   * too, past every read in flight.
   */
  conflict?: (err: unknown) => Conflict | null;
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
    canAccept: canAcceptMemory,
  },
  project: {
    accept: (row, draft) => acceptProjectMemorySuggestion(row.id, editedText(row, draft)),
    dismiss: dismissProjectMemorySuggestion,
    saveError: 'Failed to save project memory',
    canAccept: canAcceptMemory,
  },
  doc: {
    accept: (row, draft) => acceptDocNoteSuggestion(row.id, docNoteAcceptOptions(row, draft)),
    dismiss: dismissDocNoteSuggestion,
    saveError: 'Failed to save doc note',
    // A blank draft would clear a doc's instructions; one over the cap is refused.
    canAccept: canAcceptDocNote,
    // The instructions changed since the diff was rendered: re-base on the
    // current text the 409 carries, unless a list read during the accept
    // already brought the row as new, so the diff shows what accepting
    // now replaces.
    conflict: (err) => {
      if (!isStaleInstructionsError(err)) return null;
      const current = staleInstructionsCurrent(err);
      if (!current) return { rebase: null };
      return { rebase: (row) => (row.kind === 'doc' ? rebaseOnStale(row, current) : row) };
    },
  },
};

/** A row's kind API bound to the row, so callers need not narrow its kind. */
interface RowApi {
  accept: (draft: string | undefined) => Promise<{ suggestion: ResolvedFields }>;
  dismiss: () => Promise<{ suggestion: ResolvedFields }>;
  saveError: string;
  canAccept: (draft: string | undefined) => boolean;
  conflict: (err: unknown) => Conflict | null;
}

function bindRow<R extends MemoryInboxRow>(api: KindApi<R>, row: R): RowApi {
  return {
    accept: (draft) => api.accept(row, draft),
    dismiss: () => api.dismiss(row.id),
    saveError: api.saveError,
    canAccept: (draft) => api.canAccept?.(row, draft) ?? true,
    conflict: (err) => api.conflict?.(err) ?? null,
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
      {suggestion.kind === 'doc' && (
        <DocTitleLink suggestion={suggestion} classPrefix="memory-inbox-doc" onOpenDoc={onOpenDoc} />
      )}
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
  const openSectionRef = useRef<HTMLElement>(null);
  const openTitleRef = useRef<HTMLHeadingElement>(null);

  // Open-list reads are numbered as they start; one numbered below the floor
  // is dropped when it lands. Applying a read raises the floor to it (older
  // reads still in flight are stale), and an accept conflict raises it past
  // every read started so far before it re-reads.
  const readSeqRef = useRef(0);
  const readFloorRef = useRef(0);
  // Rows accepted or dismissed here, left out of every read.
  const resolvedIdsRef = useRef(new Set<string>());

  const loadPending = useCallback(async () => {
    const seq = ++readSeqRef.current;
    try {
      const resp = await fetchMemoryInboxPending();
      if (seq < readFloorRef.current) return;
      readFloorRef.current = seq;
      const rows = resp.suggestions.filter(
        (row) => isReviewableRow(row) && !resolvedIdsRef.current.has(row.id),
      );
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
      if (seq < readFloorRef.current) return;
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
      if (!suggestionCountKind(event.type)) return;
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
    resolvedIdsRef.current.add(id);
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

  // An accept refused as a conflict (the kind's ``conflict``, e.g. a doc
  // instructions accept's 409 stale_instructions): re-base the row as it is
  // in state now, then re-read the list, superseding every read already in
  // flight.
  const applyConflict = useCallback((id: string, { rebase }: Conflict) => {
    if (rebase) {
      setPending((prev) => prev && prev.map((s) => (s.id === id ? rebase(s) : s)));
    }
    readFloorRef.current = readSeqRef.current + 1;
    void loadPending();
  }, [loadPending]);

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
      // exactly what was sent: the edited text an accept sent, else the
      // proposal (an archive-only row has none and never takes a draft).
      // The response is the bare suggestion, so the inbox-only fields (and
      // a doc row's doc) stay from the row.
      const fields = {
        status: outcome.status,
        resolved_at: outcome.resolved_at,
        memory_id: outcome.memory_id,
      };
      const saved = action === 'accept' ? editedText(suggestion, draft) : undefined;
      const reviewed: MemoryInboxRow = saved === undefined
        ? { ...suggestion, ...fields }
        : { ...suggestion, ...fields, content: saved };
      setResolved((prev) => [reviewed, ...prev.filter((s) => s.id !== suggestion.id)]);
      return true;
    } catch (err) {
      // 409 suggestion_resolved: resolved from another tab -- the row is
      // gone either way; the next reviewed-list load shows the outcome.
      if (err instanceof ApiClientError && err.errorCode === 'suggestion_resolved') {
        removePending(suggestion.id);
        return true;
      }
      // Still pending but changed underneath (doc: the instructions moved):
      // the draft stays, and the next Accept goes against the re-based row.
      const conflict = action === 'accept' ? api.conflict(err) : null;
      if (conflict) applyConflict(suggestion.id, conflict);
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
  }, [drafts, removePending, applyConflict, setBusy]);

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

  useSuggestionListFocus({
    containerRef: openSectionRef,
    rowIds: pending ? pending.map((s) => s.id) : [],
    fallbackRef: openTitleRef,
  });

  return (
    <div className="memory-inbox">
      <section
        ref={openSectionRef}
        className="memory-inbox-section"
        aria-labelledby="memory-inbox-open-title"
      >
        <div className="memory-inbox-section-header">
          <h3
            ref={openTitleRef}
            id="memory-inbox-open-title"
            className="memory-inbox-section-title"
            tabIndex={-1}
          >
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
                className={`memory-inbox-reviewed memory-inbox-reviewed--${suggestion.status}`
                  + ` memory-inbox-reviewed--${suggestion.kind}`}
              >
                <div className="memory-inbox-reviewed-header">
                  <span className={`memory-inbox-status-pill memory-inbox-status-pill--${suggestion.status}`}>
                    {suggestion.status === 'accepted' ? 'Accepted' : 'Dismissed'}
                  </span>
                  {suggestion.kind === 'doc' && (
                    <span
                      className="memory-suggestion-pill memory-inbox-note-pill"
                      title={DOC_NOTE_LABELS[suggestion.note_kind].pillTitle}
                    >
                      {DOC_NOTE_LABELS[suggestion.note_kind].pill}
                    </span>
                  )}
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
