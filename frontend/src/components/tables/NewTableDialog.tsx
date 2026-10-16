/**
 * New Table dialog (spec 8.3): title, description, location and the schema
 * editor body (TableSchemaEditor's SchemaEditorBody) over an empty draft
 * with one "Name" text column. Opened from All Tables (TablesListView).
 *
 * Location: "Your tables" (a user table, always private) or one of the
 * user's projects (non-archived, plus the preselected one), preselected to
 * the project the list is filtered to. No mode is sent: a project table
 * takes its project's mode, which a line under the select names.
 *
 * Create validates the draft with the same rules as the Columns panel
 * (`validateDraft` with the title, on an empty table: the title is checked
 * trimmed and in code points, as the server counts it, so the input has no
 * maxLength) and sends one `createTable` (`draftToCreateRequest`), then
 * navigates to the new table's viewer. While Create is disabled a line
 * beside it says why: "Add a title to create the table." while the title
 * is blank, else "N problems to fix" -- a button that focuses the title
 * when it is the problem, else expands the columns with problems and
 * focuses the first (SchemaEditorBody's `revealProblems`). Refusals land at
 * their field: `duplicate_title` (and a title the server refuses) at the
 * title, a column spec error (`detail.column`, the column's name) at that
 * column, anything else (`table_limit`, `project_not_found`, ...) in a
 * banner. The host returns focus to its New Table button on close.
 *
 * Chrome follows NewDocModal (ModalShell: overlay, Escape, focus on the
 * title when it opens). Escape and a backdrop click close the dialog only
 * while nothing has been typed; with a draft, only Cancel / the close button
 * do, so a stray click never throws the columns away. The dialog scrolls as
 * one box with its header and action row pinned (NewTableDialog.css); on
 * phones it fills the screen.
 */

import { useEffect, useMemo, useRef, useState, type FormEvent } from 'react';
import { useNavigate } from 'react-router-dom';
import { createTable, tableErrorDetail } from '../../api/tablesApi';
import { ApiClientError } from '../../api/request';
import type { Project } from '../../api/types';
import {
  TABLE_DESCRIPTION_MAX_LEN,
  draftToCreateRequest,
  emptyDraft,
  validateDraft,
  type SchemaDraft,
} from '../../utils/tableSchemaOps';
import { tableViewerPath } from '../../utils/tablesRoute';
import { ModalShell } from '../ModalShell';
import { SchemaEditorBody, problemsPhrase, type SchemaEditorBodyHandle } from './TableSchemaEditor';
import '../docs/NewDocModal.css';
import './NewTableDialog.css';

/** Location select value for "Your tables" (a user table). */
const USER_TABLES_LOCATION = '';

/** Errors the user fixes by editing the title. */
const TITLE_ERRORS = new Set(['duplicate_title', 'invalid_title']);

export const ADD_TITLE_HINT = 'Add a title to create the table.';

export interface NewTableDialogProps {
  isOpen: boolean;
  onClose: () => void;
  projects: Project[];
  /** Preselected location (the filtered project), or null for "Your tables". */
  initialProjectId: string | null;
}

export function NewTableDialog({ isOpen, onClose, projects, initialProjectId }: NewTableDialogProps) {
  const navigate = useNavigate();
  const [title, setTitle] = useState('');
  const [description, setDescription] = useState('');
  const [location, setLocation] = useState(USER_TABLES_LOCATION);
  const [draft, setDraft] = useState<SchemaDraft>(emptyDraft);
  const [bodyKey, setBodyKey] = useState(0);
  const [creating, setCreating] = useState(false);
  const [titleError, setTitleError] = useState<string | null>(null);
  const [columnErrors, setColumnErrors] = useState<Record<string, string>>({});
  const [banner, setBanner] = useState<string | null>(null);
  const [touched, setTouched] = useState(false);
  const titleRef = useRef<HTMLInputElement>(null);
  const bodyRef = useRef<SchemaEditorBodyHandle>(null);

  const locationProjects = useMemo(
    () =>
      projects
        .filter((p) => !p.archived || p.id === initialProjectId)
        .sort((a, b) => a.name.toLowerCase().localeCompare(b.name.toLowerCase())),
    [projects, initialProjectId],
  );
  const initialLocation =
    initialProjectId && locationProjects.some((p) => p.id === initialProjectId)
      ? initialProjectId
      : USER_TABLES_LOCATION;

  // Reset everything each time the dialog opens.
  useEffect(() => {
    if (!isOpen) return;
    setTitle('');
    setDescription('');
    setLocation(initialLocation);
    setDraft(emptyDraft());
    setBodyKey((k) => k + 1);
    setTitleError(null);
    setColumnErrors({});
    setBanner(null);
    setTouched(false);
    const timer = setTimeout(() => titleRef.current?.focus(), 50);
    return () => clearTimeout(timer);
    // initialLocation is read on open only.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isOpen]);

  const fullDraft = useMemo(() => ({ ...draft, title }), [draft, title]);
  const errors = useMemo(
    () => validateDraft([], fullDraft, { tableEmpty: true, checkTitle: true }),
    [fullDraft],
  );
  const selectedProject =
    location === USER_TABLES_LOCATION ? null : (projects.find((p) => p.id === location) ?? null);

  const changeDraft = (next: SchemaDraft) => {
    setDraft(next);
    setTouched(true);
    setColumnErrors({});
  };

  const create = async (e: FormEvent) => {
    e.preventDefault();
    if (creating || errors.count > 0) return;
    setCreating(true);
    setTitleError(null);
    setColumnErrors({});
    setBanner(null);
    try {
      const body = draftToCreateRequest(fullDraft, {
        projectId: location === USER_TABLES_LOCATION ? null : location,
        description,
      });
      const detail = await createTable(body);
      onClose();
      navigate(tableViewerPath(detail.id));
    } catch (err) {
      const message = err instanceof Error && err.message ? err.message : 'Failed to create the table.';
      const code = err instanceof ApiClientError ? err.errorCode : undefined;
      const detail = tableErrorDetail(err);
      const columnName = typeof detail?.column === 'string' ? detail.column : null;
      const target = columnName !== null ? draft.columns.find((c) => c.name === columnName) : undefined;
      if (code && TITLE_ERRORS.has(code)) setTitleError(message);
      else if (target) setColumnErrors({ [target.id]: message });
      else setBanner(message);
    } finally {
      setCreating(false);
    }
  };

  const pristine = !touched && title === '' && description === '';
  const shownTitleError = titleError ?? (title !== '' ? errors.title : undefined);
  const titleBlank = title.trim() === '';
  // Why Create is disabled (nothing while it is enabled or busy).
  const blocked = !creating && errors.count > 0;
  const revealProblems = () => {
    if (errors.title) titleRef.current?.focus();
    else bodyRef.current?.revealProblems();
  };

  return (
    <ModalShell
      isOpen={isOpen}
      onClose={() => {
        if (pristine && !creating) onClose();
      }}
      onEscape={pristine && !creating ? onClose : null}
      overlayClassName="new-doc-overlay"
      modalClassName="new-doc-modal new-table-modal"
      ariaLabelledBy="new-table-heading"
    >
      <div className="new-doc-header">
        <h2 id="new-table-heading">New Table</h2>
        <button type="button" className="new-doc-close-button" onClick={onClose} aria-label="Close" disabled={creating}>
          <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <line x1="18" y1="6" x2="6" y2="18"></line>
            <line x1="6" y1="6" x2="18" y2="18"></line>
          </svg>
        </button>
      </div>
      <form className="new-doc-body" onSubmit={(e) => void create(e)}>
        <label htmlFor="new-table-title-input" className="new-doc-label">
          Title
        </label>
        <input
          ref={titleRef}
          id="new-table-title-input"
          type="text"
          className="new-doc-input"
          value={title}
          onChange={(e) => {
            setTitle(e.target.value);
            setTitleError(null);
          }}
          placeholder="e.g. Leads, Inventory..."
          disabled={creating}
          aria-invalid={Boolean(shownTitleError)}
          aria-describedby={shownTitleError ? 'new-table-title-error' : undefined}
        />
        {shownTitleError && (
          <div id="new-table-title-error" className="schema-editor-field-error">
            {shownTitleError}
          </div>
        )}

        <label htmlFor="new-table-description-input" className="new-doc-label new-doc-label-spaced">
          Description <span className="new-doc-optional">(optional)</span>
        </label>
        <textarea
          id="new-table-description-input"
          className="new-doc-input new-doc-textarea"
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          placeholder="What this table is for"
          maxLength={TABLE_DESCRIPTION_MAX_LEN}
          rows={2}
          disabled={creating}
        />

        <label htmlFor="new-table-location-select" className="new-doc-label new-doc-label-spaced">
          Location
        </label>
        <select
          id="new-table-location-select"
          className="new-doc-input new-doc-select"
          value={location}
          onChange={(e) => setLocation(e.target.value)}
          disabled={creating}
        >
          <option value={USER_TABLES_LOCATION}>Your tables</option>
          {locationProjects.map((p) => (
            <option key={p.id} value={p.id}>
              {p.archived ? `${p.name} (archived)` : p.name}
            </option>
          ))}
        </select>
        {selectedProject && (
          <p className="new-doc-mode-inherited">
            Mode: <strong>{selectedProject.public ? 'Public' : 'Private'}</strong> — a project table takes its
            project's mode
          </p>
        )}

        <div className="new-doc-label new-doc-label-spaced">Columns</div>
        <SchemaEditorBody
          key={bodyKey}
          ref={bodyRef}
          draft={draft}
          onChange={changeDraft}
          base={[]}
          errors={{ ...errors, title: undefined }}
          serverErrors={columnErrors}
          disabled={creating}
        />

        {banner && (
          <div className="new-doc-error" role="alert">
            {banner}
          </div>
        )}
        <div className="new-doc-actions new-table-actions">
          {blocked && (
            <div id="new-table-create-hint" className="new-table-hint" role="status">
              {titleBlank ? (
                ADD_TITLE_HINT
              ) : (
                <button type="button" className="schema-editor-problems" onClick={revealProblems}>
                  {problemsPhrase(errors.count)}
                </button>
              )}
            </div>
          )}
          <div className="new-table-buttons">
            <button type="button" className="new-doc-cancel-button" onClick={onClose} disabled={creating}>
              Cancel
            </button>
            <button
              type="submit"
              className="new-doc-create-button"
              disabled={creating || errors.count > 0}
              aria-describedby={blocked ? 'new-table-create-hint' : undefined}
            >
              {creating ? 'Creating...' : 'Create Table'}
            </button>
          </div>
        </div>
      </form>
    </ModalShell>
  );
}
