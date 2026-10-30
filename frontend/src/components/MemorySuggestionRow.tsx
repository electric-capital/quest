/**
 * One pending suggestion under review: the editable text, the word diff
 * against what accepting replaces, the model's reasoning and the Accept /
 * Dismiss buttons. Shared by the composer's review panel
 * (MemorySuggestions.tsx), the inbox's Memories tab (MemoryInbox.tsx) and,
 * for doc notes, the doc viewer's Notes panel (docs/DocNotesPanel.tsx); the
 * caller owns the draft, busy and error state so bulk actions and list
 * refreshes stay in one place.
 *
 * Kinds: ``user`` (a personal memory) and ``project`` (a project memory)
 * take a MemorySuggestion; ``doc`` takes a DocNoteSuggestion and adds a
 * header line (an Instructions / Memory pill and, unless ``showDocLink`` is
 * false, the doc's title linking to its viewer), for an instructions row the
 * diff from the doc's current instructions with a notice when they changed
 * since the proposal, and an ``n / max`` counter against the server's cap.
 * A doc row's Accept follows canAcceptDocNote (docNoteReview.ts): disabled
 * over the cap, on a blank text and while the doc is unavailable; Dismiss
 * always works.
 *
 * Styled by MemorySuggestions.css: ``.memory-suggestion-row--<kind>`` sets
 * the colour (the accent for ``user``, the project amber, the doc rose).
 */

import { useId, useLayoutEffect, useMemo, useRef } from 'react';
import type { MouseEvent, ReactNode, TextareaHTMLAttributes } from 'react';
import { Link } from 'react-router-dom';
import { Check, FileText, LoaderCircle, TriangleAlert, X } from 'lucide-react';
import type { DocNoteSuggestion, MemorySuggestion } from '../api/types';
import { docViewerPath } from '../utils/docsRoute';
import { wordDiff } from '../utils/wordDiff';
import {
  canAcceptDocNote,
  docNoteCharCount,
  docNoteMaxChars,
  isDocNoteDocAvailable,
} from './docNoteReview';

export type MemorySuggestionKind = 'user' | 'project' | 'doc';

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

/** Row wording of the two doc note kinds. */
export const DOC_NOTE_LABELS = {
  instructions: {
    pill: 'Instructions',
    pillTitle: "Instructions: the doc's rules, which models follow before they change it",
    textLabel: 'Doc instructions text',
    diffLabel: 'Changes to the instructions',
    newLabel: 'New instructions (the doc has none)',
    diffTitle: "Accepting replaces the doc's instructions with this text",
    movedNotice: 'The instructions changed since this was suggested',
  },
  memory: {
    pill: 'Memory',
    pillTitle: 'A doc memory: a fact models read before they change the doc',
    textLabel: 'Doc memory text',
    replacesLabel: 'Replaces an existing doc memory',
    replacesTitle: 'Accepting overwrites the doc memory this one updates',
  },
} as const;

const UNAVAILABLE_DOC_LABEL = 'Unavailable doc';
const UNAVAILABLE_DOC_TITLE = 'This doc was deleted or you no longer have access to it';

function formatCount(n: number): string {
  return n.toLocaleString('en-US');
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

/**
 * What accepting a replacement changes: the replaced text with the words
 * the draft drops struck through on red and the words it adds on green.
 * Follows the draft as the user edits it.
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

/**
 * A doc row's header line: the Instructions / Memory pill, then the doc's
 * title as a link to its viewer ("Unavailable doc" when the doc is gone or
 * hidden), unless the surface already names the doc.
 */
function DocNoteHeader({
  suggestion,
  showDocLink,
  onOpenDoc,
}: { suggestion: DocNoteSuggestion; showDocLink: boolean; onOpenDoc?: () => void }) {
  const labels = DOC_NOTE_LABELS[suggestion.note_kind];
  const docTitle = isDocNoteDocAvailable(suggestion) ? suggestion.doc_title : '';
  const onLinkClick = (e: MouseEvent<HTMLAnchorElement>) => {
    // A modified click opens a new tab; this view stays where it is.
    if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    onOpenDoc?.();
  };
  return (
    <div className="memory-suggestion-doc-header">
      <span className="memory-suggestion-pill" title={labels.pillTitle}>{labels.pill}</span>
      {showDocLink && (docTitle ? (
        <Link
          to={docViewerPath(suggestion.doc_id)}
          className="memory-suggestion-doc-link"
          title={docTitle}
          onClick={onLinkClick}
        >
          <FileText size={13} aria-hidden="true" />
          <span className="memory-suggestion-doc-title">{docTitle}</span>
        </Link>
      ) : (
        <span className="memory-suggestion-doc-unavailable" title={UNAVAILABLE_DOC_TITLE}>
          {UNAVAILABLE_DOC_LABEL}
        </span>
      ))}
    </div>
  );
}

/** The state the caller owns, the same for every kind. */
export interface MemorySuggestionRowStateProps {
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

interface MemoryRowProps extends MemorySuggestionRowStateProps {
  kind: 'user' | 'project';
  suggestion: MemorySuggestion;
}

interface DocRowProps extends MemorySuggestionRowStateProps {
  kind: 'doc';
  suggestion: DocNoteSuggestion;
  /** Show the doc's title as a link (default). False where the surface already names the doc. */
  showDocLink?: boolean;
  /** Called on a plain click of the doc link, before the navigation. */
  onOpenDoc?: () => void;
}

export type MemorySuggestionRowProps = MemoryRowProps | DocRowProps;

export function MemorySuggestionRow(props: MemorySuggestionRowProps) {
  const {
    kind,
    suggestion,
    draft,
    onDraftChange,
    busy,
    error,
    onAccept,
    onDismiss,
    children,
  } = props;
  const ids = useId();
  const docNote = props.kind === 'doc' ? props.suggestion : null;
  // An archive-only personal / project row (a dream saying a memory is
  // stale or merged elsewhere) has no text of its own: it shows the memory
  // it retires instead of a textarea, and Accept just archives it. A doc
  // note always has text.
  const archive = props.kind !== 'doc' && props.suggestion.content === null
    ? ROW_LABELS[props.kind]
    : null;
  const archiveOnly = archive !== null;
  // The replacement wording of a personal / project memory or a doc memory;
  // an instructions row replaces the whole field instead (its own diff).
  const replaces = props.kind === 'doc'
    ? (props.suggestion.note_kind === 'memory' ? DOC_NOTE_LABELS.memory : null)
    : ROW_LABELS[props.kind];
  const textLabel = props.kind === 'doc'
    ? DOC_NOTE_LABELS[props.suggestion.note_kind].textLabel
    : ROW_LABELS[props.kind].textLabel;
  // A replacement with the old text in hand gets the diff; one without
  // (memory deleted meanwhile) keeps the plain pill.
  const replacedText = replaces && suggestion.replaces_memory_id
    ? suggestion.replaces_content ?? null
    : null;
  const orphanReplacement = !archiveOnly
    && Boolean(replaces && suggestion.replaces_memory_id)
    && replacedText === null;
  // An instructions row diffs from the doc's current instructions (null:
  // not known, e.g. the doc is no longer visible).
  const instructions = docNote?.note_kind === 'instructions' ? docNote : null;
  const currentInstructions = instructions?.current_instructions ?? null;

  // Doc rows count against the server's cap; over it, Accept is disabled.
  const maxChars = docNote ? docNoteMaxChars(docNote.note_kind) : null;
  const charCount = maxChars !== null ? docNoteCharCount(draft) : 0;
  const overCap = maxChars !== null && charCount > maxChars;
  const counterId = `${ids}-counter`;
  const overCapId = `${ids}-over`;
  const docUnavailable = docNote !== null && !isDocNoteDocAvailable(docNote);
  // An archive-only row has no text: Accept just archives the memory.
  const acceptDisabled = busy || (docNote
    ? !canAcceptDocNote(docNote, draft)
    : !archiveOnly && !draft.trim());
  const describedBy = maxChars !== null
    ? [counterId, overCap && overCapId].filter(Boolean).join(' ')
    : undefined;

  return (
    <div
      className={`memory-suggestion-row memory-suggestion-row--${kind}${busy ? ' is-busy' : ''}`}
    >
      {props.kind === 'doc' && (
        <DocNoteHeader
          suggestion={props.suggestion}
          showDocLink={props.showDocLink ?? true}
          onOpenDoc={props.onOpenDoc}
        />
      )}
      {archive ? (
        <div className="memory-suggestion-archive" data-testid="memory-suggestion-archive">
          <div className="memory-suggestion-diff-label">
            <span title={archive.archiveTitle}>{archive.archiveLabel}</span>
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
          aria-label={textLabel}
          aria-describedby={describedBy}
          aria-invalid={overCap || undefined}
        />
      )}
      {overCap && (
        <p id={overCapId} className="memory-suggestion-over-cap">
          Use at most {formatCount(maxChars)} characters.
        </p>
      )}
      {instructions?.instructions_moved && (
        <div className="memory-suggestion-moved">
          <TriangleAlert size={13} aria-hidden="true" />
          <span>{DOC_NOTE_LABELS.instructions.movedNotice}</span>
        </div>
      )}
      {currentInstructions !== null && (
        <ReplacementDiff
          before={currentInstructions}
          after={draft}
          label={currentInstructions === ''
            ? DOC_NOTE_LABELS.instructions.newLabel
            : DOC_NOTE_LABELS.instructions.diffLabel}
          title={DOC_NOTE_LABELS.instructions.diffTitle}
        />
      )}
      {!archiveOnly && replaces && replacedText !== null && (
        <ReplacementDiff
          before={replacedText}
          after={draft}
          label={replaces.replacesLabel}
          title={replaces.replacesTitle}
        />
      )}
      {(suggestion.reasoning || orphanReplacement) && (
        <div className="memory-suggestion-meta">
          {suggestion.reasoning && (
            <span className="memory-suggestion-reasoning">{suggestion.reasoning}</span>
          )}
          {replaces && orphanReplacement && (
            <span className="memory-suggestion-replaces" title={replaces.replacesTitle}>
              {replaces.replacesLabel}
            </span>
          )}
        </div>
      )}
      {children}
      {error && (
        <div className="memory-suggestions-error" role="alert">{error}</div>
      )}
      <div className="memory-suggestion-actions">
        {maxChars !== null && (
          <span
            id={counterId}
            className={overCap
              ? 'memory-suggestion-counter memory-suggestion-counter--over'
              : 'memory-suggestion-counter'}
          >
            {formatCount(charCount)} / {formatCount(maxChars)}
            <span className="memory-suggestion-sr-only"> characters</span>
          </span>
        )}
        <button
          type="button"
          className="memory-suggestion-accept"
          onClick={onAccept}
          disabled={acceptDisabled}
          title={docUnavailable ? UNAVAILABLE_DOC_TITLE : undefined}
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
