/**
 * The doc viewer's "Notes" panel: the doc's instructions (which models
 * follow) and its memories (which models read) before they change the doc,
 * from useDocNotes (DocViewer owns the hook and passes its `view`; the panel
 * never calls the API itself).
 *
 * Contents, top to bottom. Who can do what always follows the live
 * `view.notes` rights (an editor whose right drops closes with it and its
 * draft is dropped):
 * - Suggestions for you: only while the viewer has pending suggestions for
 *   this doc (`view.suggestions`, their own rows, so for every viewer, read
 *   shares too). Each is the shared MemorySuggestionRow (kind `doc`, no doc
 *   link: the panel is the doc) with its editable text, Accept and Dismiss
 *   through the hook, which drops the row (and after an accept reloads the
 *   notes). An instructions accept sends the version its diff was rendered
 *   against (docNoteReview.ts); a 409 `stale_instructions` keeps the draft,
 *   shows the server's message and re-bases the row's diff on the current
 *   text it carries until the hook's re-fetched row is at least as new, so
 *   the next Accept is against what the user sees. A row resolved elsewhere
 *   just leaves; other errors show on the row. As rows leave, focus follows
 *   useSuggestionListFocus (the next row, else the previous one, else the
 *   panel's heading: the card's "Notes" heading, the phone section's
 *   summary). A failed suggestions load (`view.suggestionsError`) shows a
 *   line with Retry (`view.reload`) above the rows, which stay listed; the
 *   line is an alert, announced again for each failure, and a Retry that
 *   clears it hands focus (by the same hook) to the first row, else the
 *   heading. All of it sits above the empty-state line, which stays about
 *   the notes.
 * - Instructions: plain text with its line breaks. The owner (`is_owner`)
 *   gets Edit ("Add instructions" while there are none), a textarea with a
 *   character counter and Save / Cancel, and an "Edited by <name>, <time>"
 *   line; saving an empty text removes the instructions. Write shares see
 *   the text ("No instructions yet." while there is none) with a note that
 *   only the owner changes it; read shares see the text only.
 * - Memories (newest first): editors (`can_edit`: the owner and write
 *   shares) get an Add row, inline edit and a delete through
 *   DocConfirmDialog. Errors (a duplicate, the 50-memory cap, a vanished
 *   memory) show inline under the row that caused them; a duplicate also
 *   highlights the memory that already has the text. Read shares see the
 *   list, or "No memories yet.".
 *
 * A doc without notes gets one line instead: for editors, that models
 * follow its instructions and read its memories before they change it; for
 * read shares, "No notes yet.".
 *
 * Save sends the `instructions_version` the editor opened with. When the
 * instructions changed meanwhile, the 409 `stale_instructions` keeps the
 * draft and shows the server's current text above it, read-only, so the
 * owner can merge by hand; the next Save sends that current version (a
 * second 409 replaces the shown text again).
 *
 * The caps (DOC_INSTRUCTIONS_MAX_CHARS, DOC_MEMORY_MAX_CHARS) count like the
 * server: code points of the trimmed text. Over a cap the counter turns red,
 * a polite live region says so once and Save / Add is disabled; nothing is
 * truncated. The notes' three text fields share CappedTextEditor (a
 * suggestion row has its own counter).
 * Focus stays in the panel: on a failed save it returns to the field, after
 * Save / Cancel to the button that opened the editor, after an add to the
 * Add field (or, when the new memory reached the cap and the max line
 * replaced the field, to its Edit button), after a memory delete to a
 * neighbouring memory's Edit button (or the Add field), when the delete
 * confirm is dismissed to the memory's Delete button (DocConfirmDialog,
 * which keeps Tab inside it while open), and after the notes' Retry to
 * Retry while the load still fails, else to the panel's heading. A save,
 * add or Retry that settles after the user went elsewhere (focus, or a
 * pointer press, outside the editor, Add row or panel it started in)
 * leaves focus where the user put it. After a touch or pen tap no text
 * field is focused from script, as that opens the on-screen keyboard: a
 * failed save goes back to Save and a failed add to Add, a successful add
 * leaves focus where it is, and deleting the last memory focuses the
 * heading instead of the Add field.
 *
 * Two presentations of the same contents: `card` is the desktop aside's
 * floating card (the chat RightPanel's `.right-panel-card` chrome and
 * uppercase heading, the body scrolling inside it), `details` the phone's
 * collapsed "Notes" section under the document, whose summary carries a
 * rose count of the viewer's pending suggestions (the doc rose of their
 * rows), so they are not missed while it is closed. Renders nothing when
 * docNotesPanelShown is false (gate closed, doc gone, notes refused with
 * `doc_notes_disabled`).
 */

import {
  useCallback,
  useEffect,
  useId,
  useRef,
  useState,
  type ReactNode,
  type Ref,
  type RefObject,
} from 'react';
import { ChevronRight, Pencil, Plus, Trash2 } from 'lucide-react';
import {
  DOC_INSTRUCTIONS_MAX_CHARS,
  DOC_MAX_MEMORIES,
  DOC_MEMORY_MAX_CHARS,
  duplicateMemoryId,
  isStaleInstructionsError,
  staleInstructionsCurrent,
} from '../../api/docsApi';
import { ApiClientError } from '../../api/request';
import type {
  DocMemory,
  DocNotes,
  DocNoteSuggestion,
  StaleInstructionsCurrent,
} from '../../api/types';
import type { DocNotesView } from '../../hooks/useDocNotes';
import { isTouchPointer, useSuggestionListFocus } from '../../hooks/useSuggestionListFocus';
import { formatRelativeTimestamp, parseUTCTimestamp } from '../../utils/formatters';
import { MemorySuggestionRow } from '../MemorySuggestionRow';
import { docNoteAcceptOptions, instructionsCaughtUp, rebaseOnStale } from '../docNoteReview';
import { DocConfirmDialog } from './DocConfirmDialog';
import '../RightPanel.css';
import '../MemorySuggestions.css';
import './DocNotesPanel.css';

const STALE_MESSAGE =
  'The instructions changed since you started editing. Review the current text and save again.';
const DUPLICATE_MESSAGE = 'This doc already has that memory.';
const MEMORY_GONE_MESSAGE = 'This memory was deleted.';
const SUGGESTIONS_HINT = 'From your conversations. Nothing changes until you accept.';
const SUGGESTION_ACCEPT_ERROR = 'Failed to save doc note';
const SUGGESTION_DISMISS_ERROR = 'Failed to dismiss';
const SUGGESTIONS_LOAD_ERROR = 'Failed to load your suggestions for this doc.';
const EDITOR_EMPTY_LINE =
  "Models follow this doc's instructions and read its memories before they change it.";
// How long the memory a duplicate points at stays highlighted.
const HIGHLIGHT_MS = 2000;
// Code points of a memory quoted in the delete confirm.
const QUOTE_MAX_CHARS = 200;

export interface DocNotesPanelProps {
  /** One doc's notes, from useDocNotes (DocViewer owns the hook). */
  view: DocNotesView;
  /** `card` (default): the desktop aside card. `details`: the phone's collapsed section. */
  variant?: 'card' | 'details';
  /** The signed-in user's email, to say "Edited by you" (GET /me exposes no user id). */
  viewerEmail?: string | null;
}

/**
 * Whether DocNotesPanel renders anything for this view: the hook is enabled,
 * the doc was not 404, and the notes were not refused with doc_notes_disabled.
 */
export function docNotesPanelShown(view: DocNotesView): boolean {
  return view.enabled && !view.notFound && view.error?.errorCode !== 'doc_notes_disabled';
}

/** Characters as the server counts them: code points of the trimmed text. */
function charCount(text: string): number {
  return [...text.trim()].length;
}

function isOverCap(text: string, max: number): boolean {
  return charCount(text) > max;
}

function formatCount(n: number): string {
  return n.toLocaleString('en-US');
}

function pendingSuggestionsLabel(n: number): string {
  return `${formatCount(n)} pending ${n === 1 ? 'suggestion' : 'suggestions'}`;
}

function errorMessage(err: unknown, fallback: string): string {
  return err instanceof Error && err.message ? err.message : fallback;
}

/** The inline message for a failed memory add / edit. */
function memoryErrorMessage(err: unknown, fallback: string): string {
  if (err instanceof ApiClientError) {
    if (err.errorCode === 'duplicate_memory') return DUPLICATE_MESSAGE;
    if (err.errorCode === 'memory_not_found') return MEMORY_GONE_MESSAGE;
  }
  return errorMessage(err, fallback);
}

function isMemoryNotFound(err: unknown): boolean {
  return err instanceof ApiClientError && err.errorCode === 'memory_not_found';
}

/** "you" (email match, case-insensitive) or the last editor's name; null when unknown. */
function instructionsEditor(notes: DocNotes, viewerEmail: string | null): string | null {
  const user = notes.instructions_updated_by;
  if (!user || !notes.instructions_updated_at) return null;
  const isViewer =
    !!viewerEmail && !!user.email && user.email.toLowerCase() === viewerEmail.toLowerCase();
  return isViewer ? 'you' : user.name || user.email || 'a deleted user';
}

function quote(text: string): string {
  const chars = [...text];
  return chars.length > QUOTE_MAX_CHARS
    ? `${chars.slice(0, QUOTE_MAX_CHARS).join('').trimEnd()}…`
    : text;
}

// A number per error object: each failed load brings a new one, so its line
// re-mounts and is announced again.
const errorKeys = new WeakMap<object, number>();
let lastErrorKey = 0;
function errorKey(err: object): number {
  let key = errorKeys.get(err);
  if (key === undefined) {
    lastErrorKey += 1;
    key = lastErrorKey;
    errorKeys.set(err, key);
  }
  return key;
}

// --- Focus after an action ------------------------------------------------------

/**
 * The user's last input, kept by capture-phase listeners on the document
 * (useInputState): whether it was a touch or pen tap (not a mouse click or
 * a key), and the pointer presses (a count and the last one's target), so
 * an action that settles later can tell the user went elsewhere meanwhile.
 */
interface InputState {
  touch: boolean;
  presses: number;
  pressTarget: EventTarget | null;
}

function useInputState(): RefObject<InputState> {
  const ref = useRef<InputState>({ touch: false, presses: 0, pressTarget: null });
  useEffect(() => {
    const onPointerDown = (e: PointerEvent) => {
      ref.current = {
        touch: isTouchPointer(e),
        presses: ref.current.presses + 1,
        pressTarget: e.target,
      };
    };
    const onKeyDown = () => {
      ref.current = { ...ref.current, touch: false };
    };
    document.addEventListener('pointerdown', onPointerDown, true);
    document.addEventListener('keydown', onKeyDown, true);
    return () => {
      document.removeEventListener('pointerdown', onPointerDown, true);
      document.removeEventListener('keydown', onKeyDown, true);
    };
  }, []);
  return ref;
}

/** Where an action started: the presses so far and the part of the panel it belongs to. */
interface FocusMark {
  presses: number;
  scope: HTMLElement | null;
}

function focusMark(input: InputState, scope: HTMLElement | null): FocusMark {
  return { presses: input.presses, scope };
}

/** Focus dropped to <body> (or nowhere), as when the focused button was disabled or removed. */
function isFocusLost(): boolean {
  const active = document.activeElement;
  return !active || active === document.body || !active.isConnected;
}

/**
 * Focus the first of `targets` that is there, enabled and takes it, unless
 * the user went elsewhere since `mark`: focus has to be lost or still in
 * the mark's scope (which may have left the page since), and the last
 * pointer press since the mark, if any, in that scope too. After a tap,
 * text fields are skipped: focusing one from script opens the on-screen
 * keyboard.
 */
function restoreFocus(
  input: InputState,
  mark: FocusMark,
  targets: readonly (HTMLElement | null | undefined)[],
): void {
  const inScope = (node: unknown) =>
    mark.scope !== null && node instanceof Node && mark.scope.contains(node);
  if (!isFocusLost() && !inScope(document.activeElement)) return;
  if (input.presses !== mark.presses && !inScope(input.pressTarget)) return;
  for (const target of targets) {
    if (!target?.isConnected || target.matches(':disabled')) continue;
    if (input.touch && target.matches('textarea, input')) continue;
    target.focus();
    if (document.activeElement === target) return;
  }
}

// --- Capped text editor -------------------------------------------------------

interface CappedTextEditorProps {
  /** The textarea's accessible name. */
  label: string;
  value: string;
  onChange: (value: string) => void;
  /** The server's cap, counted like charCount. */
  max: number;
  rows: number;
  placeholder?: string;
  /** A save is in flight: the textarea is read-only (the actions disable themselves). */
  busy: boolean;
  /** `when-text`: no counter while the field is empty (the Add row). */
  counter?: 'always' | 'when-text';
  /** Above the textarea (the instructions' 409 merge view). */
  header?: ReactNode;
  /** The footer's buttons. */
  actions: ReactNode;
  /** Shown under the footer as an alert. */
  error: string | null;
  className?: string;
  /** The editor's root: the scope its focus marks are taken in. */
  rootRef?: Ref<HTMLDivElement>;
  textareaRef: Ref<HTMLTextAreaElement>;
}

/**
 * A textarea with its `n / max` counter, an over-cap line, the footer
 * buttons and an inline error. Over the cap the field is `aria-invalid`
 * and described by the over-cap line as well as the counter. The over-cap
 * line sits in a polite live region that stays in the DOM (empty under the
 * cap), so a screen reader hears it once as the text crosses the cap and
 * not on every keystroke.
 */
function CappedTextEditor({
  label,
  value,
  onChange,
  max,
  rows,
  placeholder,
  busy,
  counter = 'always',
  header,
  actions,
  error,
  className = 'doc-notes-editor',
  rootRef,
  textareaRef,
}: CappedTextEditorProps) {
  const ids = useId();
  const counterId = `${ids}-counter`;
  const overId = `${ids}-over`;
  const count = charCount(value);
  const overCap = count > max;
  const showCounter = counter === 'always' || value !== '';
  const describedBy = [showCounter && counterId, overCap && overId].filter(Boolean).join(' ');
  return (
    <div ref={rootRef} className={className}>
      {header}
      <textarea
        ref={textareaRef}
        className="doc-notes-input"
        aria-label={label}
        aria-describedby={describedBy || undefined}
        aria-invalid={overCap || undefined}
        placeholder={placeholder}
        rows={rows}
        value={value}
        readOnly={busy}
        onChange={(e) => onChange(e.target.value)}
      />
      <div className="doc-notes-over-cap-live" aria-live="polite">
        {overCap && (
          <p id={overId} className="doc-notes-over-cap">
            Use at most {formatCount(max)} characters.
          </p>
        )}
      </div>
      <div className="doc-notes-editor-footer">
        {showCounter && (
          <span
            id={counterId}
            className={overCap ? 'doc-notes-counter doc-notes-counter--over' : 'doc-notes-counter'}
          >
            {formatCount(count)} / {formatCount(max)}
          </span>
        )}
        <div className="doc-notes-actions">{actions}</div>
      </div>
      {error && (
        <p className="doc-notes-error" role="alert">
          {error}
        </p>
      )}
    </div>
  );
}

// --- Suggestions for you ------------------------------------------------------

/** A suggestion accepted, dismissed or deleted elsewhere: the hook drops its row. */
function isSuggestionGone(err: unknown): boolean {
  return (
    err instanceof ApiClientError &&
    (err.errorCode === 'suggestion_resolved' || err.errorCode === 'suggestion_not_found')
  );
}

/** `record` without the keys `keep` refuses (the same object when it keeps all). */
function filterRecord<T>(
  record: Record<string, T>,
  keep: (id: string, value: T) => boolean,
): Record<string, T> {
  const entries = Object.entries(record);
  if (entries.every(([id, value]) => keep(id, value))) return record;
  return Object.fromEntries(entries.filter(([id, value]) => keep(id, value)));
}

function withoutKey<T>(record: Record<string, T>, id: string): Record<string, T> {
  return id in record ? filterRecord(record, (key) => key !== id) : record;
}

/**
 * The viewer's pending suggestions for this doc, each reviewed in place,
 * under the suggestions load error while there is one. Mounted with the
 * notes, its area empty while there is nothing to show, so the per-row
 * drafts, busy flags, errors and 409 re-bases live here, keyed by
 * suggestion id, and are dropped as their rows leave `view.suggestions`;
 * and so useSuggestionListFocus, watching the area, can hand focus to the
 * panel's heading once the last row (or the error line) has left.
 */
function SuggestionsSection({
  view,
  fallbackRef,
}: {
  view: DocNotesView;
  /** The panel's heading, focused once nothing in the area is left. */
  fallbackRef: RefObject<HTMLElement | null>;
}) {
  const { suggestions, suggestionsError, acceptSuggestion, dismissSuggestion } = view;
  const areaRef = useRef<HTMLDivElement>(null);
  const { retrying, retry } = useRetry(view.reload);
  // Edited text per row (absent = the proposed content untouched).
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [busyIds, setBusyIds] = useState<ReadonlySet<string>>(() => new Set());
  const [rowErrors, setRowErrors] = useState<Record<string, string>>({});
  // A 409 stale_instructions's current instructions per row: its diff and
  // next Accept use them until the hook's re-fetched row is as new.
  const [rebases, setRebases] = useState<Record<string, StaleInstructionsCurrent>>({});

  useEffect(() => {
    const live = new Map(suggestions.map((s) => [s.id, s]));
    setDrafts((prev) => filterRecord(prev, (id) => live.has(id)));
    setRowErrors((prev) => filterRecord(prev, (id) => live.has(id)));
    setRebases((prev) =>
      filterRecord(prev, (id, current) => {
        const row = live.get(id);
        return row !== undefined && !instructionsCaughtUp(row, current);
      }),
    );
  }, [suggestions]);

  useSuggestionListFocus({
    containerRef: areaRef,
    rowIds: suggestions.map((s) => s.id),
    fallbackRef,
  });

  const setBusy = (id: string, busy: boolean) => {
    setBusyIds((prev) => {
      if (prev.has(id) === busy) return prev;
      const next = new Set(prev);
      if (busy) next.add(id);
      else next.delete(id);
      return next;
    });
  };

  const resolve = async (row: DocNoteSuggestion, action: 'accept' | 'dismiss') => {
    const { id } = row;
    if (busyIds.has(id)) return;
    setBusy(id, true);
    setRowErrors((prev) => withoutKey(prev, id));
    try {
      if (action === 'accept') {
        await acceptSuggestion(id, docNoteAcceptOptions(row, drafts[id]));
      } else {
        await dismissSuggestion(id);
      }
    } catch (err) {
      // Resolved elsewhere: what the user wanted gone is gone.
      if (isSuggestionGone(err)) return;
      // Without a current text the hook's re-fetch carries the new version.
      const current = action === 'accept' ? staleInstructionsCurrent(err) : null;
      if (current) setRebases((prev) => ({ ...prev, [id]: current }));
      setRowErrors((prev) => ({
        ...prev,
        [id]: errorMessage(
          err,
          action === 'accept' ? SUGGESTION_ACCEPT_ERROR : SUGGESTION_DISMISS_ERROR,
        ),
      }));
    } finally {
      setBusy(id, false);
    }
  };

  return (
    <div ref={areaRef} className="doc-notes-suggestions-area">
      {suggestionsError && (
        <LoadFailure
          className="doc-notes-load-error"
          message={SUGGESTIONS_LOAD_ERROR}
          announceKey={errorKey(suggestionsError)}
          retrying={retrying}
          onRetry={() => void retry()}
        />
      )}
      {suggestions.length > 0 && (
        <div className="doc-notes-section doc-notes-suggestions">
          <div className="doc-notes-section-header">
            <h4 className="doc-notes-section-heading">Suggestions for you</h4>
            <span className="doc-notes-count">{suggestions.length}</span>
          </div>
          <p className="doc-notes-hint">{SUGGESTIONS_HINT}</p>
          {suggestions.map((suggestion) => {
            const rebase = rebases[suggestion.id];
            const row = rebase ? rebaseOnStale(suggestion, rebase) : suggestion;
            return (
              <MemorySuggestionRow
                key={row.id}
                kind="doc"
                suggestion={row}
                showDocLink={false}
                draft={drafts[row.id] ?? row.content}
                onDraftChange={(text) => setDrafts((prev) => ({ ...prev, [row.id]: text }))}
                busy={busyIds.has(row.id)}
                error={rowErrors[row.id]}
                onAccept={() => void resolve(row, 'accept')}
                onDismiss={() => void resolve(row, 'dismiss')}
              />
            );
          })}
        </div>
      )}
    </div>
  );
}

// --- Instructions -------------------------------------------------------------

interface InstructionsEditorState {
  draft: string;
  /** The version Save sends: the one the editor opened with, then a 409's current one. */
  baseVersion: number;
  /**
   * Set by a 409 stale_instructions: its notice, the server's current text,
   * and a count of the 409s (it keys the notice, so each one is announced).
   */
  conflict: { message: string; current: StaleInstructionsCurrent | null; n: number } | null;
  saving: boolean;
  error: string | null;
}

function InstructionsSection({
  notes,
  panelEmpty,
  saveInstructions,
  viewerEmail,
  input,
}: {
  notes: DocNotes;
  panelEmpty: boolean;
  saveInstructions: DocNotesView['saveInstructions'];
  viewerEmail: string | null;
  input: RefObject<InputState>;
}) {
  const [editor, setEditor] = useState<InstructionsEditorState | null>(null);
  const editorRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const saveButtonRef = useRef<HTMLButtonElement>(null);
  // The Edit / "Add instructions" button (only one of them is rendered).
  const openButtonRef = useRef<HTMLButtonElement>(null);
  // Set when Save or Cancel closes the editor: focus goes back to the opener.
  const refocusOpenerRef = useRef<FocusMark | null>(null);
  // Set when a save fails: focus goes back to the draft (to Save after a tap).
  const refocusDraftRef = useRef<FocusMark | null>(null);
  const currentLabelId = useId();

  const isOwner = notes.is_owner;
  const editorOpen = editor !== null && isOwner;

  // Losing the owner right drops the draft, so a re-grant starts afresh.
  useEffect(() => {
    if (!isOwner) setEditor(null);
  }, [isOwner]);

  useEffect(() => {
    if (editorOpen) {
      const el = textareaRef.current;
      if (!el) return;
      el.focus();
      el.setSelectionRange(el.value.length, el.value.length);
    } else if (refocusOpenerRef.current) {
      const mark = refocusOpenerRef.current;
      refocusOpenerRef.current = null;
      restoreFocus(input.current, mark, [openButtonRef.current]);
    }
  }, [editorOpen, input]);

  // After a failed save, once the editor is enabled again.
  const saving = editor?.saving === true;
  useEffect(() => {
    const mark = refocusDraftRef.current;
    if (saving || !mark) return;
    refocusDraftRef.current = null;
    restoreFocus(input.current, mark, [textareaRef.current, saveButtonRef.current]);
  }, [saving, input]);

  const hasText = notes.instructions !== '';
  // Read shares see nothing here while there are no instructions.
  if (!isOwner && !notes.can_edit && !hasText) return null;

  const open = () => {
    setEditor({
      draft: notes.instructions,
      baseVersion: notes.instructions_version,
      conflict: null,
      saving: false,
      error: null,
    });
  };

  /** Close the editor; `mark` is where the save that closes it started. */
  const close = (mark?: FocusMark) => {
    refocusOpenerRef.current = mark ?? focusMark(input.current, editorRef.current);
    setEditor(null);
  };

  const overCap = editor ? isOverCap(editor.draft, DOC_INSTRUCTIONS_MAX_CHARS) : false;

  const save = async () => {
    if (!editor || editor.saving || overCap) return;
    // A 409 without a usable `current` falls back to the live notes.
    const expected =
      editor.conflict && !editor.conflict.current ? notes.instructions_version : editor.baseVersion;
    const mark = focusMark(input.current, editorRef.current);
    setEditor({ ...editor, saving: true, error: null });
    try {
      await saveInstructions(editor.draft.trim(), expected);
      close(mark);
    } catch (err) {
      refocusDraftRef.current = mark;
      if (isStaleInstructionsError(err)) {
        const current = staleInstructionsCurrent(err);
        setEditor(
          (prev) =>
            prev && {
              ...prev,
              saving: false,
              error: null,
              baseVersion: current ? current.instructions_version : prev.baseVersion,
              conflict: {
                message: err.message || STALE_MESSAGE,
                current,
                n: (prev.conflict?.n ?? 0) + 1,
              },
            },
        );
      } else {
        setEditor(
          (prev) =>
            prev && {
              ...prev,
              saving: false,
              error: errorMessage(err, 'Failed to save the instructions.'),
            },
        );
      }
    }
  };

  const editedBy = isOwner && hasText ? instructionsEditor(notes, viewerEmail) : null;

  let body: ReactNode;
  if (editorOpen && editor) {
    let header: ReactNode = null;
    if (editor.conflict) {
      const conflictText = editor.conflict.current?.instructions ?? notes.instructions;
      header = (
        <>
          <p key={editor.conflict.n} className="doc-notes-notice" role="alert">
            {editor.conflict.message}
          </p>
          <div className="doc-notes-current" role="group" aria-labelledby={currentLabelId}>
            <div id={currentLabelId} className="doc-notes-current-label">
              Current instructions
            </div>
            <div
              className={
                conflictText
                  ? 'doc-notes-current-text'
                  : 'doc-notes-current-text doc-notes-current-text--empty'
              }
            >
              {conflictText || 'The instructions were removed.'}
            </div>
          </div>
        </>
      );
    }
    body = (
      <CappedTextEditor
        label="Instructions"
        value={editor.draft}
        onChange={(draft) => setEditor((prev) => prev && { ...prev, draft })}
        max={DOC_INSTRUCTIONS_MAX_CHARS}
        rows={8}
        placeholder="How models should work on this doc"
        busy={editor.saving}
        header={header}
        error={editor.error}
        rootRef={editorRef}
        textareaRef={textareaRef}
        actions={
          <>
            <button
              type="button"
              className="doc-notes-button"
              onClick={() => close()}
              disabled={editor.saving}
            >
              Cancel
            </button>
            <button
              ref={saveButtonRef}
              type="button"
              className="doc-notes-button doc-notes-button--primary"
              onClick={() => void save()}
              disabled={editor.saving || overCap}
            >
              {editor.saving ? 'Saving...' : 'Save'}
            </button>
          </>
        }
      />
    );
  } else {
    body = (
      <>
        {hasText && <div className="doc-notes-instructions-text">{notes.instructions}</div>}
        {editedBy && notes.instructions_updated_at && (
          <p
            className="doc-notes-meta"
            title={parseUTCTimestamp(notes.instructions_updated_at).toLocaleString()}
          >
            Edited by {editedBy}, {formatRelativeTimestamp(notes.instructions_updated_at)}
          </p>
        )}
        {isOwner && !hasText && (
          <button
            ref={openButtonRef}
            type="button"
            className="doc-notes-button doc-notes-button--add"
            onClick={open}
          >
            <Plus size={14} aria-hidden="true" />
            Add instructions
          </button>
        )}
        {!isOwner && !hasText && !panelEmpty && (
          <p className="doc-notes-status">No instructions yet.</p>
        )}
        {!isOwner && notes.can_edit && (
          <p className="doc-notes-hint">Only the doc's owner can change the instructions.</p>
        )}
      </>
    );
  }

  return (
    <div className="doc-notes-section doc-notes-instructions">
      <div className="doc-notes-section-header">
        <h4 className="doc-notes-section-heading">Instructions</h4>
        {isOwner && hasText && !editorOpen && (
          <button
            ref={openButtonRef}
            type="button"
            className="doc-notes-button doc-notes-button--small"
            onClick={open}
            aria-label="Edit instructions"
          >
            Edit
          </button>
        )}
      </div>
      {body}
    </div>
  );
}

// --- Memories -----------------------------------------------------------------

interface MemoryEditState {
  id: string;
  draft: string;
  saving: boolean;
  error: string | null;
}

function MemoriesSection({
  notes,
  panelEmpty,
  view,
  input,
  headingRef,
}: {
  notes: DocNotes;
  panelEmpty: boolean;
  view: DocNotesView;
  input: RefObject<InputState>;
  /** The panel's heading: focused after a tap deletes the last memory. */
  headingRef: RefObject<HTMLElement | null>;
}) {
  const canEdit = notes.can_edit;
  const memories = notes.memories;

  const [addDraft, setAddDraft] = useState('');
  const [adding, setAdding] = useState(false);
  const [addError, setAddError] = useState<string | null>(null);
  const [edit, setEdit] = useState<MemoryEditState | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<DocMemory | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  // The memory a duplicate error points at, highlighted for a moment; `n`
  // makes a repeat of the same duplicate restart the timer.
  const [highlight, setHighlight] = useState<{ id: string; n: number } | null>(null);

  const listRef = useRef<HTMLUListElement>(null);
  const addRowRef = useRef<HTMLDivElement>(null);
  const addInputRef = useRef<HTMLTextAreaElement>(null);
  const addButtonRef = useRef<HTMLButtonElement>(null);
  const editRowRef = useRef<HTMLDivElement>(null);
  const editInputRef = useRef<HTMLTextAreaElement>(null);
  const editSaveRef = useRef<HTMLButtonElement>(null);
  const editButtonsRef = useRef(new Map<string, HTMLButtonElement>());
  // Set when Save or Cancel closes a memory editor: focus goes back to its Edit button.
  const refocusEditOfRef = useRef<{ id: string; mark: FocusMark } | null>(null);
  // Set when a memory save fails: focus goes back to the draft (to Save after a tap).
  const refocusEditDraftRef = useRef<FocusMark | null>(null);
  // Set when a delete closes the dialog: the memories whose Edit button
  // takes focus (the next one, else the previous one), else the Add field.
  const refocusAfterDeleteRef = useRef<string[] | null>(null);
  // Set when an add settles, with the new memory's id when it succeeded.
  const refocusAfterAddRef = useRef<{ addedId: string | null; mark: FocusMark } | null>(null);
  const highlightSeqRef = useRef(0);
  const ids = useId();

  // Losing the right to edit drops every draft and the delete dialog, so a
  // re-grant starts afresh.
  useEffect(() => {
    if (canEdit) return;
    setEdit(null);
    setDeleteTarget(null);
    setDeleting(false);
    setDeleteError(null);
    setAddDraft('');
    setAddError(null);
    refocusAfterDeleteRef.current = null;
  }, [canEdit]);

  // The open editor, unless its memory is gone or the right to edit dropped.
  const editingId =
    edit && canEdit && memories.some((m) => m.id === edit.id) ? edit.id : null;
  // No other memory editor opens while a memory save is in flight, so its
  // error cannot be dropped.
  const memorySaving = edit?.saving === true;

  useEffect(() => {
    if (editingId) {
      const el = editInputRef.current;
      if (!el) return;
      el.focus();
      el.setSelectionRange(el.value.length, el.value.length);
    } else if (refocusEditOfRef.current) {
      const { id, mark } = refocusEditOfRef.current;
      refocusEditOfRef.current = null;
      restoreFocus(input.current, mark, [editButtonsRef.current.get(id)]);
    }
  }, [editingId, input]);

  // After a failed memory save, once the editor is enabled again.
  useEffect(() => {
    const mark = refocusEditDraftRef.current;
    if (memorySaving || !mark) return;
    refocusEditDraftRef.current = null;
    restoreFocus(input.current, mark, [editInputRef.current, editSaveRef.current]);
  }, [memorySaving, input]);

  // After a delete closed the dialog. Runs after the dialog's own focus
  // return (child cleanups run first), whose opener is gone with the memory.
  useEffect(() => {
    if (deleteTarget !== null) return;
    const candidates = refocusAfterDeleteRef.current;
    if (!candidates) return;
    refocusAfterDeleteRef.current = null;
    for (const id of candidates) {
      const button = editButtonsRef.current.get(id);
      if (button?.isConnected) {
        button.focus();
        return;
      }
    }
    // No memory left: the Add field, or after a tap (no on-screen keyboard
    // from script) the panel's heading.
    (input.current.touch ? headingRef.current : addInputRef.current)?.focus();
  }, [deleteTarget, input, headingRef]);

  // After an add settled, with the list showing its result: back to the
  // field, to add another or to fix the draft (after a tap, to Add while it
  // is enabled, which a cleared draft is not). The memory that reached the
  // cap took the field away (the max line replaces the Add row): its Edit
  // button instead.
  useEffect(() => {
    const settled = refocusAfterAddRef.current;
    if (adding || !settled) return;
    refocusAfterAddRef.current = null;
    const targets = addInputRef.current
      ? [addInputRef.current, addButtonRef.current]
      : [settled.addedId ? editButtonsRef.current.get(settled.addedId) : null];
    restoreFocus(input.current, settled.mark, targets);
  }, [adding, input]);

  useEffect(() => {
    if (!highlight) return;
    const item = [...(listRef.current?.querySelectorAll<HTMLElement>('[data-memory-id]') ?? [])].find(
      (el) => el.dataset.memoryId === highlight.id,
    );
    // Optional: jsdom has no scrollIntoView.
    item?.scrollIntoView?.({ block: 'nearest' });
    const timer = window.setTimeout(() => setHighlight(null), HIGHLIGHT_MS);
    return () => window.clearTimeout(timer);
  }, [highlight]);

  const flashDuplicate = (err: unknown) => {
    const id = duplicateMemoryId(err);
    if (id) {
      highlightSeqRef.current += 1;
      setHighlight({ id, n: highlightSeqRef.current });
    }
  };

  const addOverCap = isOverCap(addDraft, DOC_MEMORY_MAX_CHARS);
  const atCap = memories.length >= DOC_MAX_MEMORIES;

  const add = async () => {
    const text = addDraft.trim();
    if (!text || adding || addOverCap) return;
    const mark = focusMark(input.current, addRowRef.current);
    setAdding(true);
    setAddError(null);
    let addedId: string | null = null;
    try {
      addedId = (await view.addMemory(text)).id;
      setAddDraft('');
    } catch (err) {
      setAddError(memoryErrorMessage(err, 'Failed to add the memory.'));
      flashDuplicate(err);
    } finally {
      refocusAfterAddRef.current = { addedId, mark };
      setAdding(false);
    }
  };

  const startEdit = (memory: DocMemory) => {
    if (memorySaving) return;
    setEdit({ id: memory.id, draft: memory.content, saving: false, error: null });
  };

  /** Close memory `id`'s editor; `mark` is where the save that closes it started. */
  const closeEdit = (id: string, mark?: FocusMark) => {
    refocusEditOfRef.current = { id, mark: mark ?? focusMark(input.current, editRowRef.current) };
    setEdit((prev) => (prev?.id === id ? null : prev));
  };

  const editOverCap = edit ? isOverCap(edit.draft, DOC_MEMORY_MAX_CHARS) : false;

  const saveEdit = async () => {
    if (!edit || edit.saving || editOverCap) return;
    const text = edit.draft.trim();
    if (!text) return;
    const { id } = edit;
    const mark = focusMark(input.current, editRowRef.current);
    setEdit({ ...edit, saving: true, error: null });
    try {
      await view.updateMemory(id, text);
      closeEdit(id, mark);
    } catch (err) {
      refocusEditDraftRef.current = mark;
      setEdit((prev) =>
        prev?.id === id
          ? { ...prev, saving: false, error: memoryErrorMessage(err, 'Failed to save the memory.') }
          : prev,
      );
      flashDuplicate(err);
    }
  };

  const openDelete = (memory: DocMemory) => {
    refocusAfterDeleteRef.current = null;
    setDeleteError(null);
    setDeleteTarget(memory);
  };

  const confirmDelete = async () => {
    if (!deleteTarget) return;
    const { id } = deleteTarget;
    // Neighbours as listed when the delete was confirmed.
    const index = memories.findIndex((m) => m.id === id);
    const neighbours = [memories[index + 1]?.id, index > 0 ? memories[index - 1].id : undefined];
    const closeAfterDelete = () => {
      refocusAfterDeleteRef.current = neighbours.filter((n): n is string => Boolean(n));
      setDeleteTarget(null);
    };
    setDeleting(true);
    setDeleteError(null);
    try {
      await view.deleteMemory(id);
      setDeleting(false);
      closeAfterDelete();
    } catch (err) {
      setDeleting(false);
      // Already gone: what the user asked for.
      if (isMemoryNotFound(err)) closeAfterDelete();
      else setDeleteError(errorMessage(err, 'Failed to delete the memory.'));
    }
  };

  let addRow: ReactNode = null;
  if (canEdit && atCap) {
    addRow = (
      <p className="doc-notes-hint">
        This doc has the maximum of {DOC_MAX_MEMORIES} memories. Delete one to add another.
      </p>
    );
  } else if (canEdit) {
    addRow = (
      <CappedTextEditor
        className="doc-notes-add"
        label="New memory"
        value={addDraft}
        onChange={setAddDraft}
        max={DOC_MEMORY_MAX_CHARS}
        rows={2}
        placeholder="Add a memory"
        busy={adding}
        counter="when-text"
        error={addError}
        rootRef={addRowRef}
        textareaRef={addInputRef}
        actions={
          <button
            ref={addButtonRef}
            type="button"
            className="doc-notes-button doc-notes-button--primary"
            onClick={() => void add()}
            disabled={adding || addOverCap || addDraft.trim() === ''}
          >
            {adding ? 'Adding...' : 'Add'}
          </button>
        }
      />
    );
  }

  const renderMemory = (memory: DocMemory) => {
    const classes = ['doc-notes-memory'];
    if (memory.id === highlight?.id) classes.push('doc-notes-memory--highlight');
    const contentId = `${ids}-memory-${memory.id}`;

    if (edit && memory.id === editingId) {
      classes.push('doc-notes-memory--editing');
      return (
        <li key={memory.id} className={classes.join(' ')} data-memory-id={memory.id}>
          <CappedTextEditor
            label="Edit memory"
            value={edit.draft}
            onChange={(draft) =>
              setEdit((prev) => (prev?.id === memory.id ? { ...prev, draft } : prev))
            }
            max={DOC_MEMORY_MAX_CHARS}
            rows={3}
            busy={edit.saving}
            error={edit.error}
            rootRef={editRowRef}
            textareaRef={editInputRef}
            actions={
              <>
                <button
                  type="button"
                  className="doc-notes-button"
                  onClick={() => closeEdit(memory.id)}
                  disabled={edit.saving}
                >
                  Cancel
                </button>
                <button
                  ref={editSaveRef}
                  type="button"
                  className="doc-notes-button doc-notes-button--primary"
                  onClick={() => void saveEdit()}
                  disabled={edit.saving || editOverCap || edit.draft.trim() === ''}
                >
                  {edit.saving ? 'Saving...' : 'Save'}
                </button>
              </>
            }
          />
        </li>
      );
    }

    return (
      <li key={memory.id} className={classes.join(' ')} data-memory-id={memory.id}>
        <div className="doc-notes-memory-main">
          <div id={contentId} className="doc-notes-memory-content">
            {memory.content}
          </div>
          <div
            className="doc-notes-memory-meta"
            title={parseUTCTimestamp(memory.updated_at).toLocaleString()}
          >
            {memory.source_label}{' '}
            <span className="doc-notes-memory-time">
              · {formatRelativeTimestamp(memory.updated_at)}
            </span>
          </div>
        </div>
        {canEdit && (
          <div className="doc-notes-memory-actions">
            <button
              ref={(el) => {
                if (el) editButtonsRef.current.set(memory.id, el);
                else editButtonsRef.current.delete(memory.id);
              }}
              type="button"
              className="doc-notes-icon-button"
              onClick={() => startEdit(memory)}
              disabled={memorySaving}
              aria-label="Edit memory"
              aria-describedby={contentId}
              title="Edit memory"
            >
              <Pencil size={14} aria-hidden="true" />
            </button>
            <button
              type="button"
              className="doc-notes-icon-button doc-notes-icon-button--danger"
              onClick={() => openDelete(memory)}
              aria-label="Delete memory"
              aria-describedby={contentId}
              title="Delete memory"
            >
              <Trash2 size={14} aria-hidden="true" />
            </button>
          </div>
        )}
      </li>
    );
  };

  // Read shares of an empty doc see only the panel's empty line.
  if (!canEdit && panelEmpty) return null;

  return (
    <div className="doc-notes-section doc-notes-memories">
      <div className="doc-notes-section-header">
        <h4 className="doc-notes-section-heading">Memories</h4>
        <span className="doc-notes-count">{memories.length}</span>
      </div>
      {addRow}
      {memories.length === 0 && !canEdit && <p className="doc-notes-status">No memories yet.</p>}
      {memories.length > 0 && (
        <ul ref={listRef} className="doc-notes-memory-list">
          {memories.map(renderMemory)}
        </ul>
      )}
      {canEdit && (
        <DocConfirmDialog
          isOpen={deleteTarget !== null}
          title="Delete this memory?"
          confirmLabel="Delete"
          busyLabel="Deleting..."
          tone="danger"
          busy={deleting}
          error={deleteError}
          onConfirm={() => void confirmDelete()}
          onClose={() => setDeleteTarget(null)}
        >
          <p className="doc-notes-dialog-quote">“{quote(deleteTarget?.content ?? '')}”</p>
          <p>Models will no longer read it before they change this doc.</p>
        </DocConfirmDialog>
      )}
    </div>
  );
}

// --- Panel --------------------------------------------------------------------

function NotesContent({
  view,
  notes,
  viewerEmail,
  headingRef,
  input,
}: {
  view: DocNotesView;
  notes: DocNotes;
  viewerEmail: string | null;
  /** The panel's heading (the card's "Notes", the phone section's summary). */
  headingRef: RefObject<HTMLElement | null>;
  input: RefObject<InputState>;
}) {
  const panelEmpty = notes.instructions === '' && notes.memories.length === 0;
  return (
    <div className="doc-notes-body">
      <SuggestionsSection view={view} fallbackRef={headingRef} />
      {panelEmpty && (
        <p className="doc-notes-status">{notes.can_edit ? EDITOR_EMPTY_LINE : 'No notes yet.'}</p>
      )}
      <InstructionsSection
        notes={notes}
        panelEmpty={panelEmpty}
        saveInstructions={view.saveInstructions}
        viewerEmail={viewerEmail}
        input={input}
      />
      <MemoriesSection
        notes={notes}
        panelEmpty={panelEmpty}
        view={view}
        input={input}
        headingRef={headingRef}
      />
    </div>
  );
}

/** `reload` behind a Retry button: `retrying` until the reload settles. */
function useRetry(reload: DocNotesView['reload']) {
  const [retrying, setRetrying] = useState(false);
  const retry = async () => {
    setRetrying(true);
    try {
      await reload();
    } finally {
      setRetrying(false);
    }
  };
  return { retrying, retry };
}

/**
 * A failed load's message with Retry (disabled while `retrying`). With an
 * `announceKey` the message is an alert, re-mounted (so announced again)
 * whenever the key changes.
 */
function LoadFailure({
  message,
  retrying,
  onRetry,
  className,
  announceKey,
  buttonRef,
}: {
  message: string;
  retrying: boolean;
  onRetry: () => void;
  className: string;
  announceKey?: number;
  buttonRef?: Ref<HTMLButtonElement>;
}) {
  return (
    <div className={className}>
      <p
        key={announceKey}
        className="doc-notes-status"
        role={announceKey === undefined ? undefined : 'alert'}
      >
        {message}
      </p>
      <button
        ref={buttonRef}
        type="button"
        className="doc-notes-button"
        onClick={onRetry}
        disabled={retrying}
      >
        {retrying ? 'Retrying...' : 'Retry'}
      </button>
    </div>
  );
}

export function DocNotesPanel({ view, variant = 'card', viewerEmail = null }: DocNotesPanelProps) {
  // The panel's heading, where focus goes once the last suggestion row has
  // left: the card's "Notes" heading, the phone section's summary.
  const cardHeadingRef = useRef<HTMLHeadingElement>(null);
  const summaryRef = useRef<HTMLElement>(null);
  const headingRef = variant === 'details' ? summaryRef : cardHeadingRef;
  // The card or the phone section.
  const rootRef = useRef<HTMLElement | null>(null);
  const setRoot = useCallback((el: HTMLElement | null) => {
    rootRef.current = el;
  }, []);
  const input = useInputState();
  // The notes' failed first load: Retry, its state here so that focus can
  // be put back once the reload settles, when the notes may have replaced it.
  const notesRetry = useRetry(view.reload);
  const retryButtonRef = useRef<HTMLButtonElement>(null);
  const retryMarkRef = useRef<FocusMark | null>(null);

  useEffect(() => {
    const mark = retryMarkRef.current;
    if (notesRetry.retrying || !mark) return;
    retryMarkRef.current = null;
    restoreFocus(input.current, mark, [retryButtonRef.current, headingRef.current]);
  }, [notesRetry.retrying, input, headingRef]);

  if (!docNotesPanelShown(view)) return null;

  const retryNotes = () => {
    retryMarkRef.current = focusMark(input.current, rootRef.current);
    void notesRetry.retry();
  };

  let content: ReactNode;
  if (view.notes) {
    content = (
      <NotesContent
        view={view}
        notes={view.notes}
        viewerEmail={viewerEmail}
        headingRef={headingRef}
        input={input}
      />
    );
  } else if (view.loading) {
    content = (
      <div className="doc-notes-body">
        <p className="doc-notes-status">Loading notes...</p>
      </div>
    );
  } else {
    content = (
      <LoadFailure
        className="doc-notes-body doc-notes-load-error"
        message={view.error?.message || 'Failed to load notes.'}
        retrying={notesRetry.retrying}
        onRetry={retryNotes}
        buttonRef={retryButtonRef}
      />
    );
  }

  if (variant === 'details') {
    // Counted while the block that lists them is shown (with the notes).
    const pending = view.notes ? view.suggestions.length : 0;
    return (
      <details ref={setRoot} className="doc-notes-details">
        <summary ref={summaryRef} className="doc-notes-summary">
          <ChevronRight size={14} className="doc-notes-summary-chevron" aria-hidden="true" />
          <span>Notes</span>
          {pending > 0 && (
            // In the doc rose of the suggestion rows (MemorySuggestions.css).
            <span className="doc-notes-summary-count">
              <span aria-hidden="true">{formatCount(pending)}</span>
              <span className="doc-notes-sr-only">{`, ${pendingSuggestionsLabel(pending)}`}</span>
            </span>
          )}
        </summary>
        <div className="doc-notes-details-body">{content}</div>
      </details>
    );
  }

  // Read-only viewers have nothing focusable in the notes of a long card
  // (suggestion rows and their load error's Retry, if any, sit only at its
  // top): the scroller takes focus so the keyboard can scroll it.
  const keyboardScroll = view.notes !== null && !view.notes.can_edit;
  return (
    <section ref={setRoot} className="right-panel-card doc-notes-panel" aria-label="Notes">
      <div className="doc-notes-header">
        <h3 ref={cardHeadingRef} className="doc-notes-heading" tabIndex={-1}>
          Notes
        </h3>
      </div>
      <div className="doc-notes-scroll" tabIndex={keyboardScroll ? 0 : undefined}>
        {content}
      </div>
    </section>
  );
}
