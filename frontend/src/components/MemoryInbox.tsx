/**
 * The inbox's Memories tab: every suggested memory of the user in one place.
 *
 * Open suggestions (personal ``suggest_memory`` and project
 * ``suggest_project_memory`` proposals, across all conversations) come
 * first, each with the same editable row as the composer panel plus a line
 * saying which conversation it came from. Below them, the recently reviewed
 * (accepted / dismissed) suggestions, newest first, paged in as the user
 * scrolls. The lists come from ``/app/api/memory-inbox`` (chat/memory_inbox.py);
 * accept and dismiss go through each kind's own endpoints (``KIND_API``).
 * Rows of a kind this tab has no endpoints for (the doc note suggestions,
 * ``kind: 'doc'``) are left out of both lists: never shown, counted or
 * reached by Accept all / Dismiss all.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { Brain, FolderBookmark, LoaderCircle, Moon } from 'lucide-react';
import {
  acceptMemorySuggestion,
  acceptProjectMemorySuggestion,
  dismissMemorySuggestion,
  dismissProjectMemorySuggestion,
  fetchMemoryInboxPending,
  fetchMemoryInboxResolved,
} from '../api/client';
import { ApiClientError } from '../api/request';
import type { MemoryInboxRow, MemoryInboxSuggestion, MemorySuggestion } from '../api/types';
import { persistentWebSocket } from '../services/PersistentWebSocket';
import { formatRelativeTimestamp } from '../utils/formatters';
import { MemorySuggestionRow } from './MemorySuggestionRow';
import './MemorySuggestions.css';
import './MemoryInbox.css';

/** Rows per page of the reviewed list. */
export const RESOLVED_PAGE_SIZE = 20;

const KIND_API = {
  user: {
    accept: acceptMemorySuggestion,
    dismiss: dismissMemorySuggestion,
    saveError: 'Failed to save memory',
  },
  project: {
    accept: acceptProjectMemorySuggestion,
    dismiss: dismissProjectMemorySuggestion,
    saveError: 'Failed to save project memory',
  },
} as const;

/** A row of a kind ``KIND_API`` has accept / dismiss endpoints for. */
function isReviewableRow(row: MemoryInboxRow): row is MemoryInboxSuggestion {
  return Object.hasOwn(KIND_API, row.kind);
}

interface MemoryInboxProps {
  onGoToConversation: (conversationId: string, projectId?: string | null) => void;
}

function errorMessage(err: unknown, fallback: string): string {
  if (err instanceof Error && err.message) return err.message;
  return fallback;
}

/** Where a suggestion came from: the chat (and project) it was proposed
 *  in, with a link to it, or the memory review (a dream) that proposed it. */
function SuggestionSource({
  suggestion,
  onGoToConversation,
}: { suggestion: MemoryInboxSuggestion; onGoToConversation: MemoryInboxProps['onGoToConversation'] }) {
  const KindIcon = suggestion.kind === 'project' ? FolderBookmark : Brain;
  const conversationId = suggestion.conversation_id;
  return (
    <div className="memory-inbox-source">
      <span
        className={`memory-inbox-kind memory-inbox-kind--${suggestion.kind}`}
        title={suggestion.kind === 'project'
          ? 'A project memory: kept for every chat in the project'
          : 'A personal memory: kept across all your chats'}
      >
        <KindIcon size={12} aria-hidden="true" />
        {suggestion.kind === 'project' ? 'Project memory' : 'Memory'}
      </span>
      {suggestion.source === 'dream' || conversationId === null ? (
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

export function MemoryInbox({ onGoToConversation }: MemoryInboxProps) {
  // null until the first fetch lands.
  const [pending, setPending] = useState<MemoryInboxSuggestion[] | null>(null);
  const [pendingError, setPendingError] = useState<string | null>(null);
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [busyIds, setBusyIds] = useState<Set<string>>(() => new Set());
  const [rowErrors, setRowErrors] = useState<Record<string, string>>({});
  const [isBulkBusy, setIsBulkBusy] = useState(false);

  const [resolved, setResolved] = useState<MemoryInboxSuggestion[]>([]);
  const [resolvedLoaded, setResolvedLoaded] = useState(false);
  const [resolvedError, setResolvedError] = useState<string | null>(null);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  const loadingMoreRef = useRef(false);
  const sentinelRef = useRef<HTMLDivElement>(null);

  // Bumped on every local accept / dismiss so a pending-list fetch already
  // in flight cannot resurrect the row just removed.
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

  const resolveOne = useCallback(async (
    suggestion: MemoryInboxSuggestion,
    action: 'accept' | 'dismiss',
  ): Promise<boolean> => {
    const api = KIND_API[suggestion.kind];
    setBusy(suggestion.id, true);
    setRowErrors((prev) => {
      if (!(suggestion.id in prev)) return prev;
      const next = { ...prev };
      delete next[suggestion.id];
      return next;
    });
    try {
      let outcome: MemorySuggestion;
      if (action === 'accept') {
        const draft = drafts[suggestion.id];
        // An archive-only row has no text to edit, so it never sends one.
        const edited = suggestion.content !== null
          && draft !== undefined && draft.trim() !== suggestion.content.trim();
        outcome = (await api.accept(suggestion.id, edited ? draft.trim() : undefined)).suggestion;
      } else {
        outcome = (await api.dismiss(suggestion.id)).suggestion;
      }
      removePending(suggestion.id);
      // The row moves to the top of the reviewed list right away, showing
      // the text as saved (the edited draft on an accept). The response is
      // the bare suggestion, so the inbox-only fields stay from the row.
      const reviewed: MemoryInboxSuggestion = {
        ...suggestion,
        status: outcome.status,
        resolved_at: outcome.resolved_at,
        memory_id: outcome.memory_id,
        content: action === 'accept' && drafts[suggestion.id] !== undefined
          ? drafts[suggestion.id].trim()
          : suggestion.content,
      };
      setResolved((prev) => [reviewed, ...prev.filter((s) => s.id !== suggestion.id)]);
      return true;
    } catch (err) {
      // 409 suggestion_resolved: resolved from another tab -- the row is
      // gone either way; the next reviewed-list load shows the outcome.
      if (err instanceof ApiClientError && err.errorCode === 'suggestion_resolved') {
        removePending(suggestion.id);
        return true;
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
  }, [drafts, removePending, setBusy]);

  const resolveAll = useCallback(async (action: 'accept' | 'dismiss') => {
    if (isBulkBusy || !pending) return;
    setIsBulkBusy(true);
    try {
      // Sequential so a failure leaves the rest intact for a retry.
      for (const suggestion of pending) {
        if (busyIds.has(suggestion.id)) continue;
        await resolveOne(suggestion, action);
      }
    } finally {
      setIsBulkBusy(false);
    }
  }, [isBulkBusy, pending, busyIds, resolveOne]);

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
          Memories the assistant proposed in your chats, and clean-ups from the
          periodic review of your saved memories. Edit the text if you like,
          then accept to save it or dismiss it. Nothing changes until you
          accept.
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
            {pending.map((suggestion) => (
              <MemorySuggestionRow
                key={suggestion.id}
                suggestion={suggestion}
                kind={suggestion.kind}
                draft={drafts[suggestion.id] ?? suggestion.content ?? ''}
                onDraftChange={(text) => setDrafts((prev) => ({ ...prev, [suggestion.id]: text }))}
                busy={busyIds.has(suggestion.id)}
                error={rowErrors[suggestion.id]}
                onAccept={() => resolveOne(suggestion, 'accept')}
                onDismiss={() => resolveOne(suggestion, 'dismiss')}
              >
                <SuggestionSource suggestion={suggestion} onGoToConversation={onGoToConversation} />
              </MemorySuggestionRow>
            ))}
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
                <SuggestionSource suggestion={suggestion} onGoToConversation={onGoToConversation} />
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
