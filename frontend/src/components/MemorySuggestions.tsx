/**
 * Suggested-memories bubble + review panel, rendered above the composer.
 *
 * The model's ``suggest_memory`` tool registers proposals on the side of a
 * conversation (chat/memory_suggestions.py); nothing is saved until the
 * user accepts one here. The bubble shows the conversation's pending count
 * (kept fresh by ``memory_suggestions_changed`` events via useConversation);
 * clicking it opens a panel that overlays the conversation, where each
 * suggestion can be edited, accepted (creating the memory) or dismissed.
 */

import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import type { TextareaHTMLAttributes } from 'react';
import { Brain, Check, ChevronDown, ChevronUp, LoaderCircle, X } from 'lucide-react';
import {
  acceptMemorySuggestion,
  dismissMemorySuggestion,
  fetchMemorySuggestions,
} from '../api/client';
import { ApiClientError } from '../api/request';
import type { MemorySuggestion } from '../api/types';
import { conversationStore } from '../store/conversationStore';
import { wordDiff } from '../utils/wordDiff';
import './MemorySuggestions.css';

interface MemorySuggestionsProps {
  conversationId: string;
  /** Pending count from the store; the bubble hides at zero. */
  pendingCount: number;
}

/** Textarea that grows with its (wrapped) content instead of clipping it. */
function AutoGrowTextarea(props: TextareaHTMLAttributes<HTMLTextAreaElement>) {
  const ref = useRef<HTMLTextAreaElement>(null);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = `${el.scrollHeight}px`;
  }, [props.value]);
  return <textarea ref={ref} rows={1} {...props} />;
}

function errorMessage(err: unknown, fallback: string): string {
  if (err instanceof Error && err.message) return err.message;
  return fallback;
}

/**
 * What accepting a replacement changes: the replaced memory's text with
 * the words the draft drops struck through on red and the words it adds on
 * green. Follows the draft as the user edits it.
 */
function ReplacementDiff({ before, after }: { before: string; after: string }) {
  const segments = useMemo(() => wordDiff(before, after.trim()), [before, after]);
  const unchanged = segments.every((s) => s.type === 'equal');
  return (
    <div className="memory-suggestion-diff" data-testid="memory-suggestion-diff">
      <div className="memory-suggestion-diff-label">
        <span title="Accepting archives the memory this one updates">
          Replaces an existing memory
        </span>
        {unchanged ? (
          <span className="memory-suggestion-diff-note">(same text)</span>
        ) : (
          <span className="memory-suggestion-diff-legend" aria-hidden="true">
            <span className="memory-suggestion-diff-del">removed</span>
            <span className="memory-suggestion-diff-add">added</span>
          </span>
        )}
      </div>
      <div className="memory-suggestion-diff-body">
        {segments.map((segment, i) => {
          if (segment.type === 'equal') return <span key={i}>{segment.text}</span>;
          // The whitespace before a changed word is spacing, not a change.
          const [, lead, text] = /^(\s*)([\s\S]*)$/.exec(segment.text)!;
          return (
            <span key={i}>
              {lead}
              <span className={`memory-suggestion-diff-${segment.type}`}>{text}</span>
            </span>
          );
        })}
      </div>
    </div>
  );
}

export function MemorySuggestions({ conversationId, pendingCount }: MemorySuggestionsProps) {
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
    fetchMemorySuggestions(conversationId)
      .then((resp) => {
        if (cancelled || generation !== listGenerationRef.current) return;
        // The list is the source of truth for the count too: it reconciles
        // the bubble after a dropped socket missed an event.
        conversationStore.setPendingMemorySuggestions(conversationId, resp.pending_count);
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
  }, [isOpen, conversationId, pendingCount]);

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
        await acceptMemorySuggestion(suggestion.id, edited ? draft.trim() : undefined);
      } else {
        await dismissMemorySuggestion(suggestion.id);
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
          action === 'accept' ? 'Failed to save memory' : 'Failed to dismiss',
        ),
      }));
      return false;
    } finally {
      setBusy(suggestion.id, false);
    }
  }, [drafts, removeRow, setBusy]);

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

  const countLabel = pendingCount === 1
    ? '1 suggested memory'
    : `${pendingCount} suggested memories`;

  return (
    <div className="memory-suggestions">
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
            aria-labelledby="memory-suggestions-title"
          >
            <div className="memory-suggestions-header">
              <Brain size={16} className="memory-suggestions-header-icon" aria-hidden="true" />
              <h3 id="memory-suggestions-title" className="memory-suggestions-title">
                Suggested memories
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
                aria-label="Close suggested memories"
                title="Close"
              >
                <X size={16} />
              </button>
            </div>
            <p className="memory-suggestions-hint">
              The assistant thought these were worth remembering across chats. Edit
              the text if you like, then accept to save it as a memory or dismiss it.
              Nothing is saved until you accept.
            </p>
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
              {suggestions.map((suggestion) => {
                const busy = busyIds.has(suggestion.id);
                const draft = drafts[suggestion.id] ?? suggestion.content;
                const rowError = rowErrors[suggestion.id];
                // A replacement with the old text in hand gets the diff; one
                // without (memory deleted meanwhile) keeps the plain pill.
                const replacedText = suggestion.replaces_memory_id
                  ? suggestion.replaces_content ?? null
                  : null;
                return (
                  <div
                    key={suggestion.id}
                    className={`memory-suggestion-row${busy ? ' is-busy' : ''}`}
                  >
                    <AutoGrowTextarea
                      className="memory-suggestion-content"
                      value={draft}
                      onChange={(e) => setDrafts((prev) => ({ ...prev, [suggestion.id]: e.target.value }))}
                      disabled={busy}
                      aria-label="Memory text"
                    />
                    {replacedText !== null && (
                      <ReplacementDiff before={replacedText} after={draft} />
                    )}
                    {(suggestion.reasoning || (suggestion.replaces_memory_id && replacedText === null)) && (
                      <div className="memory-suggestion-meta">
                        {suggestion.reasoning && (
                          <span className="memory-suggestion-reasoning">{suggestion.reasoning}</span>
                        )}
                        {suggestion.replaces_memory_id && replacedText === null && (
                          <span className="memory-suggestion-replaces" title="Accepting archives the memory this one updates">
                            Replaces an existing memory
                          </span>
                        )}
                      </div>
                    )}
                    {rowError && (
                      <div className="memory-suggestions-error" role="alert">{rowError}</div>
                    )}
                    <div className="memory-suggestion-actions">
                      <button
                        type="button"
                        className="memory-suggestion-accept"
                        onClick={() => resolveOne(suggestion, 'accept')}
                        disabled={busy || !draft.trim()}
                      >
                        {busy ? (
                          <LoaderCircle size={14} className="memory-suggestions-spinner" aria-hidden="true" />
                        ) : (
                          <Check size={14} aria-hidden="true" />
                        )}
                        Accept
                      </button>
                      <button
                        type="button"
                        className="memory-suggestion-dismiss"
                        onClick={() => resolveOne(suggestion, 'dismiss')}
                        disabled={busy}
                      >
                        <X size={14} aria-hidden="true" />
                        Dismiss
                      </button>
                    </div>
                  </div>
                );
              })}
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
        <Brain size={14} aria-hidden="true" />
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
