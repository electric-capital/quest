/**
 * One pending memory suggestion under review: the editable text, the word
 * diff against the memory it replaces, the model's reasoning and the
 * Accept / Dismiss buttons. Shared by the composer's review panel
 * (MemorySuggestions.tsx) and the inbox's Memories tab (MemoryInbox.tsx);
 * the caller owns the draft, busy and error state so bulk actions and
 * list refreshes stay in one place. Styled by MemorySuggestions.css, with
 * ``kind="project"`` switching the accent to the project-memory amber.
 */

import { useLayoutEffect, useMemo, useRef } from 'react';
import type { ReactNode, TextareaHTMLAttributes } from 'react';
import { Check, LoaderCircle, X } from 'lucide-react';
import type { MemorySuggestion } from '../api/types';
import { wordDiff } from '../utils/wordDiff';

export type MemorySuggestionKind = 'user' | 'project';

/** Row wording that differs between personal and project memories. */
export const ROW_LABELS = {
  user: {
    textLabel: 'Memory text',
    replacesLabel: 'Replaces an existing memory',
    replacesTitle: 'Accepting archives the memory this one updates',
    archiveLabel: 'Archives this memory',
    archiveTitle: 'Accepting archives this memory; nothing new is saved',
  },
  project: {
    textLabel: 'Project memory text',
    replacesLabel: 'Replaces an existing project memory',
    replacesTitle: 'Accepting archives the project memory this one updates',
    archiveLabel: 'Archives this project memory',
    archiveTitle: 'Accepting archives this project memory; nothing new is saved',
  },
} as const;

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

/**
 * What accepting a replacement changes: the replaced memory's text with
 * the words the draft drops struck through on red and the words it adds on
 * green. Follows the draft as the user edits it.
 */
function ReplacementDiff({
  before,
  after,
  label,
  title,
}: { before: string; after: string; label: string; title: string }) {
  const segments = useMemo(() => wordDiff(before, after.trim()), [before, after]);
  const unchanged = segments.every((s) => s.type === 'equal');
  return (
    <div className="memory-suggestion-diff" data-testid="memory-suggestion-diff">
      <div className="memory-suggestion-diff-label">
        <span title={title}>{label}</span>
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

interface MemorySuggestionRowProps {
  suggestion: MemorySuggestion;
  kind: MemorySuggestionKind;
  /** The text in the textarea (the proposed content until edited). */
  draft: string;
  onDraftChange: (text: string) => void;
  busy: boolean;
  error?: string;
  onAccept: () => void;
  onDismiss: () => void;
  /** Extra context rendered above the actions (the inbox's "from" line). */
  children?: ReactNode;
}

export function MemorySuggestionRow({
  suggestion,
  kind,
  draft,
  onDraftChange,
  busy,
  error,
  onAccept,
  onDismiss,
  children,
}: MemorySuggestionRowProps) {
  const labels = ROW_LABELS[kind];
  // An archive-only row (a dream saying a memory is stale or merged
  // elsewhere) has no text of its own: it shows the memory it retires
  // instead of a textarea, and Accept just archives it.
  const archiveOnly = suggestion.content === null;
  // A replacement with the old text in hand gets the diff; one without
  // (memory deleted meanwhile) keeps the plain pill.
  const replacedText = suggestion.replaces_memory_id
    ? suggestion.replaces_content ?? null
    : null;
  const orphanReplacement = !archiveOnly
    && Boolean(suggestion.replaces_memory_id) && replacedText === null;
  return (
    <div
      className={`memory-suggestion-row memory-suggestion-row--${kind}${busy ? ' is-busy' : ''}`}
    >
      {archiveOnly ? (
        <div className="memory-suggestion-archive" data-testid="memory-suggestion-archive">
          <div className="memory-suggestion-diff-label">
            <span title={labels.archiveTitle}>{labels.archiveLabel}</span>
          </div>
          {replacedText !== null ? (
            <div className="memory-suggestion-archive-body">{replacedText}</div>
          ) : (
            <div className="memory-suggestion-archive-body memory-suggestion-archive-gone">
              This memory is already gone; accepting changes nothing.
            </div>
          )}
        </div>
      ) : (
        <AutoGrowTextarea
          className="memory-suggestion-content"
          value={draft}
          onChange={(e) => onDraftChange(e.target.value)}
          disabled={busy}
          aria-label={labels.textLabel}
        />
      )}
      {!archiveOnly && replacedText !== null && (
        <ReplacementDiff
          before={replacedText}
          after={draft}
          label={labels.replacesLabel}
          title={labels.replacesTitle}
        />
      )}
      {(suggestion.reasoning || orphanReplacement) && (
        <div className="memory-suggestion-meta">
          {suggestion.reasoning && (
            <span className="memory-suggestion-reasoning">{suggestion.reasoning}</span>
          )}
          {orphanReplacement && (
            <span className="memory-suggestion-replaces" title={labels.replacesTitle}>
              {labels.replacesLabel}
            </span>
          )}
        </div>
      )}
      {children}
      {error && (
        <div className="memory-suggestions-error" role="alert">{error}</div>
      )}
      <div className="memory-suggestion-actions">
        <button
          type="button"
          className="memory-suggestion-accept"
          onClick={onAccept}
          disabled={busy || (!archiveOnly && !draft.trim())}
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
          onClick={onDismiss}
          disabled={busy}
        >
          <X size={14} aria-hidden="true" />
          Dismiss
        </button>
      </div>
    </div>
  );
}
