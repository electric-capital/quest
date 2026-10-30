/**
 * The doc viewer's "Notes" panel: the doc's instructions (which models
 * follow) and its memories (which models read) before they change the doc,
 * from useDocNotes (DocViewer owns the hook and passes its `view`; the panel
 * never calls the API itself).
 *
 * Who can do what, always from the live `view.notes` rights (an editor
 * whose right drops closes with it):
 * - Instructions: plain text with its line breaks. The owner (`is_owner`)
 *   gets Edit ("Add instructions" while there are none), a textarea with a
 *   character counter and Save / Cancel, and an "Edited by <name>, <time>"
 *   line; saving an empty text removes the instructions. Write shares see
 *   the text with a note that only the owner changes it; read shares see the
 *   text only.
 * - Memories (newest first): editors (`can_edit`: the owner and write
 *   shares) get an Add row, inline edit and a delete through
 *   DocConfirmDialog. Errors (a duplicate, the 50-memory cap, a vanished
 *   memory) show inline under the row that caused them.
 *
 * Save sends the `instructions_version` the editor opened with. When the
 * instructions changed meanwhile, the 409 `stale_instructions` keeps the
 * draft and shows the server's current text above it, read-only, so the
 * owner can merge by hand; the next Save sends that current version (a
 * second 409 replaces the shown text again).
 *
 * The caps (DOC_INSTRUCTIONS_MAX_CHARS, DOC_MEMORY_MAX_CHARS) count like the
 * server: code points of the trimmed text. Over a cap the counter turns red
 * and Save / Add is disabled; nothing is truncated.
 *
 * Two presentations of the same contents: `card` is the desktop aside's
 * floating card (the chat RightPanel's `.right-panel-card` chrome and
 * uppercase heading, the body scrolling inside it), `details` the phone's
 * collapsed "Notes" section under the document. Renders nothing when
 * docNotesPanelShown is false (gate closed, doc gone, notes refused with
 * `doc_notes_disabled`).
 */

import { useEffect, useId, useRef, useState, type ReactNode } from 'react';
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
import type { DocMemory, DocNotes, StaleInstructionsCurrent } from '../../api/types';
import type { DocNotesView } from '../../hooks/useDocNotes';
import { formatRelativeTimestamp, parseUTCTimestamp } from '../../utils/formatters';
import { DocConfirmDialog } from './DocConfirmDialog';
import '../RightPanel.css';
import './DocNotesPanel.css';

const STALE_MESSAGE =
  'The instructions changed since you started editing. Review the current text and save again.';
const DUPLICATE_MESSAGE = 'This doc already has that memory.';
const MEMORY_GONE_MESSAGE = 'This memory was deleted.';
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

function formatCount(n: number): string {
  return n.toLocaleString('en-US');
}

function errorMessage(err: unknown, fallback: string): string {
  return err instanceof Error && err.message ? err.message : fallback;
}

/** The inline message for a failed memory add / edit / delete. */
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

/** `n / max` under a textarea; red once over the cap. */
function CapCounter({ id, count, max }: { id: string; count: number; max: number }) {
  const over = count > max;
  return (
    <span id={id} className={over ? 'doc-notes-counter doc-notes-counter--over' : 'doc-notes-counter'}>
      {formatCount(count)} / {formatCount(max)}
    </span>
  );
}

/** Under the textarea while it is over the cap (nothing is truncated). */
function OverCapLine({ id, max }: { id: string; max: number }) {
  return (
    <p id={id} className="doc-notes-over-cap">
      Use at most {formatCount(max)} characters.
    </p>
  );
}

// --- Instructions -------------------------------------------------------------

interface InstructionsEditorState {
  draft: string;
  /** The version Save sends: the one the editor opened with, then a 409's current one. */
  baseVersion: number;
  /** Set by a 409 stale_instructions: its notice and the server's current text. */
  conflict: { message: string; current: StaleInstructionsCurrent | null } | null;
  saving: boolean;
  error: string | null;
}

function InstructionsSection({
  notes,
  saveInstructions,
  viewerEmail,
}: {
  notes: DocNotes;
  saveInstructions: DocNotesView['saveInstructions'];
  viewerEmail: string | null;
}) {
  const [editor, setEditor] = useState<InstructionsEditorState | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  // The Edit / "Add instructions" button (only one of them is rendered).
  const openButtonRef = useRef<HTMLButtonElement>(null);
  // Set when Save or Cancel closes the editor: focus goes back to the opener.
  const refocusOpenerRef = useRef(false);
  const ids = useId();
  const counterId = `${ids}-counter`;
  const overId = `${ids}-over`;
  const currentLabelId = `${ids}-current`;

  const isOwner = notes.is_owner;
  const editorOpen = editor !== null && isOwner;

  useEffect(() => {
    if (editorOpen) {
      const el = textareaRef.current;
      if (!el) return;
      el.focus();
      el.setSelectionRange(el.value.length, el.value.length);
    } else if (refocusOpenerRef.current) {
      refocusOpenerRef.current = false;
      openButtonRef.current?.focus();
    }
  }, [editorOpen]);

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

  const close = () => {
    refocusOpenerRef.current = true;
    setEditor(null);
  };

  const count = editor ? charCount(editor.draft) : 0;
  const overCap = count > DOC_INSTRUCTIONS_MAX_CHARS;

  const save = async () => {
    if (!editor || editor.saving || overCap) return;
    // A 409 without a usable `current` falls back to the live notes.
    const expected =
      editor.conflict && !editor.conflict.current ? notes.instructions_version : editor.baseVersion;
    setEditor({ ...editor, saving: true, error: null });
    try {
      await saveInstructions(editor.draft.trim(), expected);
      close();
    } catch (err) {
      if (isStaleInstructionsError(err)) {
        const current = staleInstructionsCurrent(err);
        setEditor(
          (prev) =>
            prev && {
              ...prev,
              saving: false,
              error: null,
              baseVersion: current ? current.instructions_version : prev.baseVersion,
              conflict: { message: err.message || STALE_MESSAGE, current },
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

  const editedBy = hasText ? instructionsEditor(notes, viewerEmail) : null;

  let body: ReactNode;
  if (editorOpen && editor) {
    const conflictText = editor.conflict
      ? (editor.conflict.current?.instructions ?? notes.instructions)
      : null;
    body = (
      <div className="doc-notes-editor">
        {editor.conflict && (
          <>
            <p className="doc-notes-notice" role="alert">
              {editor.conflict.message}
            </p>
            <div className="doc-notes-current" role="group" aria-labelledby={currentLabelId}>
              <div id={currentLabelId} className="doc-notes-current-label">
                Current instructions
              </div>
              {conflictText ? (
                <div className="doc-notes-current-text">{conflictText}</div>
              ) : (
                <div className="doc-notes-current-text doc-notes-current-text--empty">
                  The instructions were removed.
                </div>
              )}
            </div>
          </>
        )}
        <textarea
          ref={textareaRef}
          className="doc-notes-input"
          aria-label="Instructions"
          aria-describedby={overCap ? `${counterId} ${overId}` : counterId}
          placeholder="How models should work on this doc"
          rows={8}
          value={editor.draft}
          readOnly={editor.saving}
          onChange={(e) => {
            const draft = e.target.value;
            setEditor((prev) => prev && { ...prev, draft });
          }}
        />
        {overCap && <OverCapLine id={overId} max={DOC_INSTRUCTIONS_MAX_CHARS} />}
        <div className="doc-notes-editor-footer">
          <CapCounter id={counterId} count={count} max={DOC_INSTRUCTIONS_MAX_CHARS} />
          <div className="doc-notes-actions">
            <button
              type="button"
              className="doc-notes-button"
              onClick={close}
              disabled={editor.saving}
            >
              Cancel
            </button>
            <button
              type="button"
              className="doc-notes-button doc-notes-button--primary"
              onClick={() => void save()}
              disabled={editor.saving || overCap}
            >
              {editor.saving ? 'Saving...' : 'Save'}
            </button>
          </div>
        </div>
        {editor.error && (
          <p className="doc-notes-error" role="alert">
            {editor.error}
          </p>
        )}
      </div>
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
}: {
  notes: DocNotes;
  panelEmpty: boolean;
  view: DocNotesView;
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
  // The memory a duplicate error points at, highlighted for a moment.
  const [highlightId, setHighlightId] = useState<string | null>(null);

  const addInputRef = useRef<HTMLTextAreaElement>(null);
  const editInputRef = useRef<HTMLTextAreaElement>(null);
  const editButtonsRef = useRef(new Map<string, HTMLButtonElement>());
  // Set when Save or Cancel closes a memory editor: focus goes back to its Edit button.
  const refocusEditOfRef = useRef<string | null>(null);
  const ids = useId();

  // The open editor, unless its memory is gone or the right to edit dropped.
  const editingId =
    edit && canEdit && memories.some((m) => m.id === edit.id) ? edit.id : null;

  useEffect(() => {
    if (editingId) {
      const el = editInputRef.current;
      if (!el) return;
      el.focus();
      el.setSelectionRange(el.value.length, el.value.length);
    } else if (refocusEditOfRef.current) {
      const id = refocusEditOfRef.current;
      refocusEditOfRef.current = null;
      editButtonsRef.current.get(id)?.focus();
    }
  }, [editingId]);

  useEffect(() => {
    if (!highlightId) return;
    const timer = window.setTimeout(() => setHighlightId(null), HIGHLIGHT_MS);
    return () => window.clearTimeout(timer);
  }, [highlightId]);

  const addCount = charCount(addDraft);
  const addOverCap = addCount > DOC_MEMORY_MAX_CHARS;
  const atCap = memories.length >= DOC_MAX_MEMORIES;

  const add = async () => {
    const text = addDraft.trim();
    if (!text || adding || addOverCap) return;
    setAdding(true);
    setAddError(null);
    try {
      await view.addMemory(text);
      setAddDraft('');
      addInputRef.current?.focus();
    } catch (err) {
      setAddError(memoryErrorMessage(err, 'Failed to add the memory.'));
      const duplicate = duplicateMemoryId(err);
      if (duplicate) setHighlightId(duplicate);
    } finally {
      setAdding(false);
    }
  };

  const startEdit = (memory: DocMemory) => {
    setEdit({ id: memory.id, draft: memory.content, saving: false, error: null });
  };

  const closeEdit = (id: string) => {
    refocusEditOfRef.current = id;
    setEdit((prev) => (prev?.id === id ? null : prev));
  };

  const editCount = edit ? charCount(edit.draft) : 0;
  const editOverCap = editCount > DOC_MEMORY_MAX_CHARS;

  const saveEdit = async () => {
    if (!edit || edit.saving || editOverCap) return;
    const text = edit.draft.trim();
    if (!text) return;
    const { id } = edit;
    setEdit({ ...edit, saving: true, error: null });
    try {
      await view.updateMemory(id, text);
      closeEdit(id);
    } catch (err) {
      setEdit((prev) =>
        prev?.id === id
          ? { ...prev, saving: false, error: memoryErrorMessage(err, 'Failed to save the memory.') }
          : prev,
      );
      const duplicate = duplicateMemoryId(err);
      if (duplicate) setHighlightId(duplicate);
    }
  };

  const openDelete = (memory: DocMemory) => {
    setDeleteError(null);
    setDeleteTarget(memory);
  };

  const confirmDelete = async () => {
    if (!deleteTarget) return;
    setDeleting(true);
    setDeleteError(null);
    try {
      await view.deleteMemory(deleteTarget.id);
      setDeleting(false);
      setDeleteTarget(null);
    } catch (err) {
      setDeleting(false);
      // Already gone: what the user asked for.
      if (isMemoryNotFound(err)) setDeleteTarget(null);
      else setDeleteError(errorMessage(err, 'Failed to delete the memory.'));
    }
  };

  const addCounterId = `${ids}-add-counter`;
  const addOverId = `${ids}-add-over`;
  const editCounterId = `${ids}-edit-counter`;
  const editOverId = `${ids}-edit-over`;

  let addRow: ReactNode = null;
  if (canEdit && atCap) {
    addRow = (
      <p className="doc-notes-hint">
        This doc has the maximum of {DOC_MAX_MEMORIES} memories. Delete one to add another.
      </p>
    );
  } else if (canEdit) {
    const describedBy = [addDraft !== '' && addCounterId, addOverCap && addOverId]
      .filter(Boolean)
      .join(' ');
    addRow = (
      <div className="doc-notes-add">
        <textarea
          ref={addInputRef}
          className="doc-notes-input"
          aria-label="New memory"
          aria-describedby={describedBy || undefined}
          placeholder="Add a memory"
          rows={2}
          value={addDraft}
          readOnly={adding}
          onChange={(e) => setAddDraft(e.target.value)}
        />
        {addOverCap && <OverCapLine id={addOverId} max={DOC_MEMORY_MAX_CHARS} />}
        <div className="doc-notes-editor-footer">
          {addDraft !== '' && (
            <CapCounter id={addCounterId} count={addCount} max={DOC_MEMORY_MAX_CHARS} />
          )}
          <div className="doc-notes-actions">
            <button
              type="button"
              className="doc-notes-button doc-notes-button--primary"
              onClick={() => void add()}
              disabled={adding || addOverCap || addDraft.trim() === ''}
            >
              {adding ? 'Adding...' : 'Add'}
            </button>
          </div>
        </div>
        {addError && (
          <p className="doc-notes-error" role="alert">
            {addError}
          </p>
        )}
      </div>
    );
  }

  const renderMemory = (memory: DocMemory) => {
    const classes = ['doc-notes-memory'];
    if (canEdit) classes.push('doc-notes-memory--editable');
    if (memory.id === highlightId) classes.push('doc-notes-memory--highlight');

    if (edit && memory.id === editingId) {
      return (
        <li key={memory.id} className={`${classes.join(' ')} doc-notes-memory--editing`}>
          <div className="doc-notes-editor">
            <textarea
              ref={editInputRef}
              className="doc-notes-input"
              aria-label="Edit memory"
              aria-describedby={editOverCap ? `${editCounterId} ${editOverId}` : editCounterId}
              rows={3}
              value={edit.draft}
              readOnly={edit.saving}
              onChange={(e) => {
                const draft = e.target.value;
                setEdit((prev) => (prev?.id === memory.id ? { ...prev, draft } : prev));
              }}
            />
            {editOverCap && <OverCapLine id={editOverId} max={DOC_MEMORY_MAX_CHARS} />}
            <div className="doc-notes-editor-footer">
              <CapCounter id={editCounterId} count={editCount} max={DOC_MEMORY_MAX_CHARS} />
              <div className="doc-notes-actions">
                <button
                  type="button"
                  className="doc-notes-button"
                  onClick={() => closeEdit(memory.id)}
                  disabled={edit.saving}
                >
                  Cancel
                </button>
                <button
                  type="button"
                  className="doc-notes-button doc-notes-button--primary"
                  onClick={() => void saveEdit()}
                  disabled={edit.saving || editOverCap || edit.draft.trim() === ''}
                >
                  {edit.saving ? 'Saving...' : 'Save'}
                </button>
              </div>
            </div>
            {edit.error && (
              <p className="doc-notes-error" role="alert">
                {edit.error}
              </p>
            )}
          </div>
        </li>
      );
    }

    return (
      <li key={memory.id} className={classes.join(' ')}>
        <div className="doc-notes-memory-main">
          <div className="doc-notes-memory-content">{memory.content}</div>
          <div
            className="doc-notes-memory-meta"
            title={parseUTCTimestamp(memory.updated_at).toLocaleString()}
          >
            {memory.source_label} · {formatRelativeTimestamp(memory.updated_at)}
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
              aria-label="Edit memory"
              title="Edit memory"
            >
              <Pencil size={14} aria-hidden="true" />
            </button>
            <button
              type="button"
              className="doc-notes-icon-button doc-notes-icon-button--danger"
              onClick={() => openDelete(memory)}
              aria-label="Delete memory"
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
      {memories.length === 0 && !canEdit && <p className="doc-notes-status">No memories.</p>}
      {memories.length > 0 && <ul className="doc-notes-memory-list">{memories.map(renderMemory)}</ul>}
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
}: {
  view: DocNotesView;
  notes: DocNotes;
  viewerEmail: string | null;
}) {
  const panelEmpty = notes.instructions === '' && notes.memories.length === 0;
  return (
    <div className="doc-notes-body">
      {/* "Suggestions for you" (phase B3) mounts here, above Instructions:
          the viewer's pending suggestions for this doc (view.suggestions). */}
      {panelEmpty && (
        <p className="doc-notes-status">
          {notes.can_edit
            ? "Models follow this doc's instructions and read its memories before they change it."
            : 'This doc has no notes.'}
        </p>
      )}
      <InstructionsSection
        notes={notes}
        saveInstructions={view.saveInstructions}
        viewerEmail={viewerEmail}
      />
      <MemoriesSection notes={notes} panelEmpty={panelEmpty} view={view} />
    </div>
  );
}

export function DocNotesPanel({ view, variant = 'card', viewerEmail = null }: DocNotesPanelProps) {
  if (!docNotesPanelShown(view)) return null;

  let content: ReactNode;
  if (view.notes) {
    content = <NotesContent view={view} notes={view.notes} viewerEmail={viewerEmail} />;
  } else if (view.loading) {
    content = (
      <div className="doc-notes-body">
        <p className="doc-notes-status">Loading notes...</p>
      </div>
    );
  } else {
    content = (
      <div className="doc-notes-body doc-notes-load-error">
        <p className="doc-notes-status">{view.error?.message || 'Failed to load notes.'}</p>
        <button type="button" className="doc-notes-button" onClick={() => void view.reload()}>
          Retry
        </button>
      </div>
    );
  }

  if (variant === 'details') {
    return (
      <details className="doc-notes-details">
        <summary className="doc-notes-summary">
          <ChevronRight size={14} className="doc-notes-summary-chevron" aria-hidden="true" />
          <span>Notes</span>
        </summary>
        <div className="doc-notes-details-body">{content}</div>
      </details>
    );
  }

  return (
    <section className="right-panel-card doc-notes-panel" aria-label="Notes">
      <div className="doc-notes-header">
        <h3 className="doc-notes-heading">Notes</h3>
      </div>
      <div className="doc-notes-scroll">{content}</div>
    </section>
  );
}
