/**
 * Suggested-memories bubble + review panel, rendered above the composer.
 *
 * The model's ``suggest_memory`` tool registers proposals on the side of a
 * conversation (chat/memory_suggestions.py); nothing is saved until the
 * user accepts one here. The bubble shows the conversation's pending count
 * (kept fresh by ``memory_suggestions_changed`` events via useConversation);
 * clicking it opens a panel that overlays the conversation, where each
 * suggestion can be edited, accepted (creating the memory) or dismissed.
 * The same rows, across every conversation, make up the inbox's Memories
 * tab (MemoryInbox.tsx).
 *
 * ``kind="project"`` renders the project-memory twin for the model's
 * ``suggest_project_memory`` proposals (chat/project_memories.py): same
 * mechanics against the project-memory endpoints, but its own amber colour,
 * folder icon and wording, so the two bubbles never read as one.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { Brain, ChevronDown, ChevronUp, FolderBookmark, LoaderCircle, X } from 'lucide-react';
import {
  acceptMemorySuggestion,
  acceptProjectMemorySuggestion,
  dismissMemorySuggestion,
  dismissProjectMemorySuggestion,
  fetchMemorySuggestions,
  fetchProjectMemorySuggestions,
} from '../api/client';
import { ApiClientError } from '../api/request';
import type { MemorySuggestion } from '../api/types';
import { conversationStore } from '../store/conversationStore';
import { MemorySuggestionRow } from './MemorySuggestionRow';
import type { MemorySuggestionKind } from './MemorySuggestionRow';
import './MemorySuggestions.css';

export type { MemorySuggestionKind };

interface MemorySuggestionsProps {
  conversationId: string;
  /** Pending count from the store; the bubble hides at zero. */
  pendingCount: number;
  /** Personal memories (default) or the conversation's project memories. */
  kind?: MemorySuggestionKind;
}

/** Everything that differs between the two kinds. */
const KIND_CONFIG = {
  user: {
    Icon: Brain,
    title: 'Suggested memories',
    countLabel: (n: number) => (n === 1 ? '1 suggested memory' : `${n} suggested memories`),
    hint: 'The assistant thought these were worth remembering across chats. Edit '
      + 'the text if you like, then accept to save it as a memory or dismiss it. '
      + 'Nothing is saved until you accept.',
    saveError: 'Failed to save memory',
    fetchList: fetchMemorySuggestions,
    accept: acceptMemorySuggestion,
    dismiss: dismissMemorySuggestion,
    setCount: (id: string, n: number) => conversationStore.setPendingMemorySuggestions(id, n),
  },
  project: {
    Icon: FolderBookmark,
    title: 'Suggested project memories',
    countLabel: (n: number) => (
      n === 1 ? '1 suggested project memory' : `${n} suggested project memories`
    ),
    hint: 'The assistant thought these facts about this project are worth keeping '
      + 'for every chat in it. Edit the text if you like, then accept to save it '
      + 'to the project\'s memories or dismiss it. Nothing is saved until you accept.',
    saveError: 'Failed to save project memory',
    fetchList: fetchProjectMemorySuggestions,
    accept: acceptProjectMemorySuggestion,
    dismiss: dismissProjectMemorySuggestion,
    setCount: (id: string, n: number) => conversationStore.setPendingProjectMemorySuggestions(id, n),
  },
} as const;

function errorMessage(err: unknown, fallback: string): string {
  if (err instanceof Error && err.message) return err.message;
  return fallback;
}

export function MemorySuggestions({
  conversationId,
  pendingCount,
  kind = 'user',
}: MemorySuggestionsProps) {
  const config = KIND_CONFIG[kind];
  const { Icon } = config;
  const titleId = `memory-suggestions-title-${kind}`;
  const [isOpen, setIsOpen] = useState(false);
  const [suggestions, setSuggestions] = useState<MemorySuggestion[]>([]);
  const [isLoading, setIsLoading] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  // Per-suggestion edited text (absent = the proposed content untouched).
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [busyIds, setBusyIds] = useState<Set<string>>(() => new Set());
  const [rowErrors, setRowErrors] = useState<Record<string, string>>({});
  const [isBulkBusy, setIsBulkBusy] = useState(false);
  const panelRef = useRef<HTMLDivElement>(null);
  // Bumped on every local accept / dismiss so a list fetch that was already
  // in flight cannot land afterwards and resurrect the row just removed.
  const listGenerationRef = useRef(0);

  // A new conversation starts closed.
  useEffect(() => {
    setIsOpen(false);
    setSuggestions([]);
    setDrafts({});
    setRowErrors({});
  }, [conversationId]);

  // Load on open and whenever the count changes while open (the count is
  // the store's view of the ``memory_suggestions_changed`` stream, so a
  // suggestion registered mid-turn or resolved in another tab refreshes
  // the list without its own subscription).
  useEffect(() => {
    if (!isOpen) return;
    let cancelled = false;
    const generation = listGenerationRef.current;
    setIsLoading(true);
    setLoadError(null);
    config.fetchList(conversationId)
      .then((resp) => {
        if (cancelled || generation !== listGenerationRef.current) return;
        // The list is the source of truth for the count too: it reconciles
        // the bubble after a dropped socket missed an event.
        config.setCount(conversationId, resp.pending_count);
        setSuggestions(resp.suggestions);
        setDrafts((prev) => {
          const live = new Set(resp.suggestions.map((s) => s.id));
          const next: Record<string, string> = {};
          for (const [id, text] of Object.entries(prev)) {
            if (live.has(id)) next[id] = text;
          }
          return next;
        });
      })
      .catch((err) => {
        if (cancelled) return;
        setLoadError(errorMessage(err, 'Failed to load suggestions'));
      })
      .finally(() => {
        if (!cancelled) setIsLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [isOpen, conversationId, pendingCount, config]);

  // Close once the last suggestion is gone (accepted / dismissed here or
  // elsewhere); the bubble disappears with it.
  useEffect(() => {
    if (isOpen && pendingCount === 0 && !isLoading) setIsOpen(false);
  }, [isOpen, pendingCount, isLoading]);

  // Escape closes the panel; capture phase so nothing underneath sees it.
  useEffect(() => {
    if (!isOpen) return;
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.stopPropagation();
        setIsOpen(false);
      }
    };
    document.addEventListener('keydown', onKeyDown, true);
    return () => document.removeEventListener('keydown', onKeyDown, true);
  }, [isOpen]);

  const removeRow = useCallback((id: string) => {
    listGenerationRef.current += 1;
    setSuggestions((prev) => prev.filter((s) => s.id !== id));
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

  const setBusy = useCallback((id: string, busy: boolean) => {
    setBusyIds((prev) => {
      const next = new Set(prev);
      if (busy) next.add(id);
      else next.delete(id);
      return next;
    });
  }, []);

  const resolveOne = useCallback(async (
    suggestion: MemorySuggestion,
    action: 'accept' | 'dismiss',
  ): Promise<boolean> => {
    setBusy(suggestion.id, true);
    setRowErrors((prev) => {
      if (!(suggestion.id in prev)) return prev;
      const next = { ...prev };
      delete next[suggestion.id];
      return next;
    });
    try {
      if (action === 'accept') {
        const draft = drafts[suggestion.id];
        const edited = draft !== undefined && draft.trim() !== suggestion.content.trim();
        await config.accept(suggestion.id, edited ? draft.trim() : undefined);
      } else {
        await config.dismiss(suggestion.id);
      }
      removeRow(suggestion.id);
      return true;
    } catch (err) {
      // 409 suggestion_resolved: resolved from another tab -- the row is
      // gone either way.
      if (err instanceof ApiClientError && err.errorCode === 'suggestion_resolved') {
        removeRow(suggestion.id);
        return true;
      }
      setRowErrors((prev) => ({
        ...prev,
        [suggestion.id]: errorMessage(
          err,
          action === 'accept' ? config.saveError : 'Failed to dismiss',
        ),
      }));
      return false;
    } finally {
      setBusy(suggestion.id, false);
    }
  }, [drafts, removeRow, setBusy, config]);

  const resolveAll = useCallback(async (action: 'accept' | 'dismiss') => {
    if (isBulkBusy) return;
    setIsBulkBusy(true);
    try {
      // Sequential so a failure leaves the rest intact for a retry.
      for (const suggestion of suggestions) {
        if (busyIds.has(suggestion.id)) continue;
        await resolveOne(suggestion, action);
      }
    } finally {
      setIsBulkBusy(false);
    }
  }, [isBulkBusy, suggestions, busyIds, resolveOne]);

  if (pendingCount <= 0 && !isOpen) return null;

  const countLabel = config.countLabel(pendingCount);

  return (
    <div className={`memory-suggestions memory-suggestions--${kind}`}>
      {isOpen && (
        <>
          <div
            className="memory-suggestions-backdrop"
            onClick={() => setIsOpen(false)}
            aria-hidden="true"
          />
          <div
            ref={panelRef}
            className="memory-suggestions-panel"
            role="dialog"
            aria-labelledby={titleId}
          >
            <div className="memory-suggestions-header">
              <Icon size={16} className="memory-suggestions-header-icon" aria-hidden="true" />
              <h3 id={titleId} className="memory-suggestions-title">
                {config.title}
              </h3>
              <span className="memory-suggestions-count" aria-hidden="true">
                {suggestions.length}
              </span>
              <div className="memory-suggestions-spacer" />
              {suggestions.length > 1 && (
                <>
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
                </>
              )}
              <button
                type="button"
                className="memory-suggestions-close"
                onClick={() => setIsOpen(false)}
                aria-label={`Close ${config.title.toLowerCase()}`}
                title="Close"
              >
                <X size={16} />
              </button>
            </div>
            <p className="memory-suggestions-hint">{config.hint}</p>
            <div className="memory-suggestions-list">
              {loadError && (
                <div className="memory-suggestions-error" role="alert">{loadError}</div>
              )}
              {isLoading && suggestions.length === 0 && !loadError && (
                <div className="memory-suggestions-empty">
                  <LoaderCircle size={16} className="memory-suggestions-spinner" aria-hidden="true" />
                  Loading…
                </div>
              )}
              {!isLoading && !loadError && suggestions.length === 0 && (
                <div className="memory-suggestions-empty">No pending suggestions.</div>
              )}
              {suggestions.map((suggestion) => (
                <MemorySuggestionRow
                  key={suggestion.id}
                  suggestion={suggestion}
                  kind={kind}
                  draft={drafts[suggestion.id] ?? suggestion.content}
                  onDraftChange={(text) => setDrafts((prev) => ({ ...prev, [suggestion.id]: text }))}
                  busy={busyIds.has(suggestion.id)}
                  error={rowErrors[suggestion.id]}
                  onAccept={() => resolveOne(suggestion, 'accept')}
                  onDismiss={() => resolveOne(suggestion, 'dismiss')}
                />
              ))}
            </div>
          </div>
        </>
      )}
      <button
        type="button"
        className={`memory-suggestions-bubble${isOpen ? ' is-open' : ''}`}
        onClick={() => setIsOpen((open) => !open)}
        aria-expanded={isOpen}
        aria-haspopup="dialog"
      >
        <Icon size={14} aria-hidden="true" />
        <span>{countLabel}</span>
        {isOpen ? (
          <ChevronDown size={14} aria-hidden="true" />
        ) : (
          <ChevronUp size={14} aria-hidden="true" />
        )}
      </button>
    </div>
  );
}
