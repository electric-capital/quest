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
 *
 * ``kind="doc"`` is the doc-note twin for ``suggest_doc_note`` proposals
 * (instructions or memories of one doc each): its endpoints are in
 * api/docsApi.ts, its colour a fixed rose, its icon the sidebar's Docs icon
 * (FileText), and its count ``pendingDocNoteSuggestions``. An instructions
 * accept sends the version its diff was rendered against
 * (docNoteReview.ts); when the instructions changed meanwhile, the 409
 * ``stale_instructions`` re-bases the row on the current text it carries
 * (unless the row in state is already as new), keeps the draft, shows the
 * server's message and re-fetches the list, so the next Accept is against
 * what the user sees and the count matches the list again.
 *
 * ``MemorySuggestionsBar`` is the row ChatPanel renders above the composer:
 * the three bubbles side by side, personal, project, doc.
 */

import { Fragment, useCallback, useEffect, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { Brain, ChevronDown, ChevronUp, FileText, FolderBookmark, LoaderCircle, X } from 'lucide-react';
import type { LucideIcon } from 'lucide-react';
import {
  acceptMemorySuggestion,
  acceptProjectMemorySuggestion,
  dismissMemorySuggestion,
  dismissProjectMemorySuggestion,
  fetchMemorySuggestions,
  fetchProjectMemorySuggestions,
} from '../api/client';
import {
  acceptDocNoteSuggestion,
  dismissDocNoteSuggestion,
  fetchConversationDocNoteSuggestions,
  isStaleInstructionsError,
  staleInstructionsCurrent,
} from '../api/docsApi';
import { ApiClientError } from '../api/request';
import type { DocNoteSuggestion, MemorySuggestion } from '../api/types';
import { conversationStore } from '../store/conversationStore';
import { MemorySuggestionRow } from './MemorySuggestionRow';
import type { MemorySuggestionKind, MemorySuggestionRowStateProps } from './MemorySuggestionRow';
import { canAcceptDocNote, docNoteAcceptOptions, withCurrentInstructions } from './docNoteReview';
import './MemorySuggestions.css';

export type { MemorySuggestionKind };

interface MemorySuggestionsProps {
  conversationId: string;
  /** Pending count from the store; the bubble hides at zero. */
  pendingCount: number;
  /** Personal memories (default), the conversation's project memories, or doc notes. */
  kind?: MemorySuggestionKind;
}

/** Everything that differs between the kinds, for rows of type ``S``. */
interface KindConfig<S extends MemorySuggestion | DocNoteSuggestion> {
  Icon: LucideIcon;
  title: string;
  countLabel: (n: number) => string;
  hint: string;
  saveError: string;
  fetchList: (conversationId: string) => Promise<{ suggestions: S[]; pending_count: number }>;
  /** Accept with the row's draft (undefined = the proposed text, never edited). */
  accept: (suggestion: S, draft: string | undefined) => Promise<unknown>;
  dismiss: (suggestionId: string) => Promise<unknown>;
  setCount: (conversationId: string, n: number) => void;
  /** Accept all skips a row this refuses (absent = every row is tried). */
  canAccept?: (suggestion: S, draft: string | undefined) => boolean;
  /**
   * A failed accept that is a conflict (the row stays pending but changed
   * underneath): ``rebase`` maps the row as it is in state when the error
   * lands (null: the error carries no data); null for an ordinary failure.
   * A conflict always re-fetches the list too.
   */
  conflict?: (err: unknown) => { rebase: ((row: S) => S) | null } | null;
  renderRow: (suggestion: S, state: MemorySuggestionRowStateProps) => ReactNode;
}

/**
 * The edited text to send, or undefined while the draft keeps the proposal.
 * An archive-only row (no content) has no text to edit, so it never sends one.
 */
function editedText(suggestion: MemorySuggestion, draft: string | undefined): string | undefined {
  if (suggestion.content === null) return undefined;
  const edited = draft !== undefined && draft.trim() !== suggestion.content.trim();
  return edited ? draft.trim() : undefined;
}

const KIND_CONFIG: {
  user: KindConfig<MemorySuggestion>;
  project: KindConfig<MemorySuggestion>;
  doc: KindConfig<DocNoteSuggestion>;
} = {
  user: {
    Icon: Brain,
    title: 'Suggested memories',
    countLabel: (n: number) => (n === 1 ? '1 suggested memory' : `${n} suggested memories`),
    hint: 'The assistant thought these were worth remembering across chats. Edit '
      + 'the text if you like, then accept to save it as a memory or dismiss it. '
      + 'Nothing is saved until you accept.',
    saveError: 'Failed to save memory',
    fetchList: fetchMemorySuggestions,
    accept: (s, draft) => acceptMemorySuggestion(s.id, editedText(s, draft)),
    dismiss: dismissMemorySuggestion,
    setCount: (id: string, n: number) => conversationStore.setPendingMemorySuggestions(id, n),
    renderRow: (s, state) => <MemorySuggestionRow kind="user" suggestion={s} {...state} />,
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
    accept: (s, draft) => acceptProjectMemorySuggestion(s.id, editedText(s, draft)),
    dismiss: dismissProjectMemorySuggestion,
    setCount: (id: string, n: number) => conversationStore.setPendingProjectMemorySuggestions(id, n),
    renderRow: (s, state) => <MemorySuggestionRow kind="project" suggestion={s} {...state} />,
  },
  doc: {
    Icon: FileText,
    title: 'Suggested doc notes',
    countLabel: (n: number) => (n === 1 ? '1 suggested doc note' : `${n} suggested doc notes`),
    hint: 'The assistant suggested instructions or memories for docs in this chat. '
      + 'Edit the text if you like, then accept to save it to the doc\'s notes or '
      + 'dismiss it. Nothing is saved until you accept.',
    saveError: 'Failed to save doc note',
    fetchList: fetchConversationDocNoteSuggestions,
    accept: (s, draft) => acceptDocNoteSuggestion(s.id, docNoteAcceptOptions(s, draft)),
    dismiss: dismissDocNoteSuggestion,
    setCount: (id: string, n: number) => conversationStore.setPendingDocNoteSuggestions(id, n),
    canAccept: canAcceptDocNote,
    conflict: (err) => {
      if (!isStaleInstructionsError(err)) return null;
      const current = staleInstructionsCurrent(err);
      if (!current) return { rebase: null };
      return {
        // A list fetch that landed during the accept may already be as new.
        rebase: (row) => (
          typeof row.current_instructions_version === 'number'
          && row.current_instructions_version >= current.instructions_version
            ? row
            : withCurrentInstructions(row, current)
        ),
      };
    },
    renderRow: (s, state) => <MemorySuggestionRow kind="doc" suggestion={s} {...state} />,
  },
};

function errorMessage(err: unknown, fallback: string): string {
  if (err instanceof Error && err.message) return err.message;
  return fallback;
}

export function MemorySuggestions({
  conversationId,
  pendingCount,
  kind = 'user',
}: MemorySuggestionsProps) {
  // Keyed by kind: the list state of one kind never meets the other's rows.
  return kind === 'doc' ? (
    <SuggestionsReview
      key={kind}
      conversationId={conversationId}
      pendingCount={pendingCount}
      kind={kind}
      config={KIND_CONFIG.doc}
    />
  ) : (
    <SuggestionsReview
      key={kind}
      conversationId={conversationId}
      pendingCount={pendingCount}
      kind={kind}
      config={KIND_CONFIG[kind]}
    />
  );
}

/** A conversation's pending suggestion counts, one per kind (useConversation). */
export interface PendingSuggestionCounts {
  user: number;
  project: number;
  doc: number;
}

interface MemorySuggestionsBarProps {
  conversationId: string;
  pending: PendingSuggestionCounts;
}

/**
 * The bubbles above the composer, side by side in a fixed order: personal
 * memories, project memories, doc notes, each hidden at zero; nothing at
 * all while the three counts are zero. ChatPanel renders it as a direct
 * child of ``.chat-panel``, which the phone rule in MemorySuggestions.css
 * (``.chat-panel > .memory-suggestions-bar``) positions over the composer.
 * The doc bubble is not gated by the doc notes feature: rows already filed
 * stay reviewable with the gate closed.
 */
export function MemorySuggestionsBar({ conversationId, pending }: MemorySuggestionsBarProps) {
  if (pending.user <= 0 && pending.project <= 0 && pending.doc <= 0) return null;
  return (
    <div className="memory-suggestions-bar">
      <MemorySuggestions conversationId={conversationId} pendingCount={pending.user} />
      <MemorySuggestions
        conversationId={conversationId}
        pendingCount={pending.project}
        kind="project"
      />
      <MemorySuggestions conversationId={conversationId} pendingCount={pending.doc} kind="doc" />
    </div>
  );
}

interface SuggestionsReviewProps<S extends MemorySuggestion | DocNoteSuggestion> {
  conversationId: string;
  pendingCount: number;
  kind: MemorySuggestionKind;
  config: KindConfig<S>;
}

function SuggestionsReview<S extends MemorySuggestion | DocNoteSuggestion>({
  conversationId,
  pendingCount,
  kind,
  config,
}: SuggestionsReviewProps<S>) {
  const { Icon } = config;
  const titleId = `memory-suggestions-title-${kind}`;
  const [isOpen, setIsOpen] = useState(false);
  const [suggestions, setSuggestions] = useState<S[]>([]);
  const [isLoading, setIsLoading] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  // Bumped to re-fetch the list while open (after an accept conflict, which
  // publishes no count event of its own).
  const [reloadTick, setReloadTick] = useState(0);
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
  }, [isOpen, conversationId, pendingCount, config, reloadTick]);

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
    suggestion: S,
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
        await config.accept(suggestion, drafts[suggestion.id]);
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
      // Still pending but changed underneath (doc: the instructions moved):
      // re-base the row now in state, keeping the draft, show why, and
      // re-fetch (the re-run cancels a fetch already in flight).
      const conflict = action === 'accept' && config.conflict ? config.conflict(err) : null;
      if (conflict) {
        const { rebase } = conflict;
        if (rebase) {
          setSuggestions((prev) => prev.map((s) => (s.id === suggestion.id ? rebase(s) : s)));
        }
        setReloadTick((n) => n + 1);
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
        if (
          action === 'accept'
          && config.canAccept
          && !config.canAccept(suggestion, drafts[suggestion.id])
        ) {
          continue;
        }
        await resolveOne(suggestion, action);
      }
    } finally {
      setIsBulkBusy(false);
    }
  }, [isBulkBusy, suggestions, busyIds, drafts, config, resolveOne]);

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
                <Fragment key={suggestion.id}>
                  {config.renderRow(suggestion, {
                    draft: drafts[suggestion.id] ?? suggestion.content ?? '',
                    onDraftChange: (text) => setDrafts((prev) => ({ ...prev, [suggestion.id]: text })),
                    busy: busyIds.has(suggestion.id),
                    error: rowErrors[suggestion.id],
                    onAccept: () => resolveOne(suggestion, 'accept'),
                    onDismiss: () => resolveOne(suggestion, 'dismiss'),
                  })}
                </Fragment>
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
