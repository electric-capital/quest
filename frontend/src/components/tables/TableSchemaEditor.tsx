/**
 * Quest Tables schema editor (spec 8.3): the Columns side panel of the table
 * viewer, and the editing surface the New Table dialog reuses.
 *
 * `SchemaEditorBody` (presentational) edits a `SchemaDraft`
 * (utils/tableSchemaOps.ts): an ordered list of column cards, each with a
 * header row (name and type summary, Move up / Move down buttons whose
 * aria-labels name the column, Delete -- disabled for the last column) and a
 * collapsible form: name; type (fixed once the column exists: the select is
 * disabled with "Type can't be changed after creation"); description;
 * Required; Unique (text, number and URL columns only); precision (number,
 * 0..10); a typed default input; for a choice (enum) column the options
 * editor (add / rename / remove; Remove is disabled for the last option and
 * for an option the default uses) and "Allow several options" (single ->
 * multi only: disabled with a note once an existing column allows several).
 * The server has no operation that reorders existing options, so options
 * move (up / down) only on a column the draft adds; new options on an
 * existing column are appended, and one that re-types an option removed
 * from it moves back into that option's place when its field loses focus
 * (`settleReaddedOptions`: the option and its rows are kept). "Add column"
 * is disabled, with a note, at TABLE_MAX_COLUMNS.
 *
 * Moving keeps keyboard focus: after a Move up / Move down (columns, or a
 * new column's options) focus returns to the same button on the moved item,
 * or to the opposite one when the item reached an end and the button is
 * disabled, and a polite live region says "<name> moved to position n of
 * m". Deleting a column moves focus to the card that takes its place (the
 * new last one at the end), removing an option to the option input that
 * takes its place (else Add option), so focus never falls to the page,
 * where Escape would no longer reach the panel; the live region says what
 * went. Every `validateDraft` problem shows at its field; a collapsed card
 * with a problem also says "Has problems" under its header (a red border
 * alone would be colour only), and `revealProblems()` on the ref handle
 * (SchemaEditorBodyHandle) expands every card with a problem and focuses
 * the first problem's field -- the hosts' "N problems to fix" buttons call
 * it. Problems never expand a card on their own (no jumps while typing). A
 * server error mapped to a column (an operation's `op_index`) shows under
 * that column's header and keeps the card open. Which cards are expanded is
 * the body's own state, or the host's (`expanded` / `onExpandedChange`), so
 * it survives the host re-basing the draft.
 *
 * `TableSchemaEditor` is the Columns panel for one loaded table (focus moves
 * to its heading when it opens). Without `access.can_edit_schema` it is
 * read-only: the columns and their settings as text plus
 * `access.schema_deny_reason` (or "Only the owner can change columns"). For
 * the owner it holds the draft and the schema it was based on (`base`:
 * version, title, columns). Save derives the operations
 * (`deriveSchemaChange`) and sends ONE `updateTableSchema` with
 * `expected_schema_version` = the base version; Save is disabled while the
 * draft has problems, no operations or more than the server takes in one
 * change (TABLE_SCHEMA_MAX_OPERATIONS). The footer counts edited things, not
 * operations (`schemaChangePhrase`: "2 columns changed, new column order"),
 * or offers "N problems to fix" (revealProblems). On success the returned
 * detail goes to the viewer (`onDetailApplied`, i.e. useTable.applyDetail),
 * the draft is re-based on the new schema in place (expanded cards stay
 * open, a saved new column's card follows it to its key) and the panel
 * stays open with a "Saved: ..." line. Whenever an action removes the
 * focused control (Save turning disabled, a banner button, Discard), focus
 * moves to the panel heading.
 *
 * Conflicts. A 409 `stale_schema` (or the viewer's schema_version moving
 * while the panel has edits) shows "This table's columns changed while you
 * were editing." with "Reload and keep my edits" (`rebaseDraft`: the edits
 * are re-applied where their column / option still exists by key; the rest
 * are listed as dropped) and "Discard my edits"; the viewer is asked to
 * re-fetch (`onRefresh`). A version move with no local edits re-bases
 * silently. A 400 `rows_violate` shows the server's message (it already
 * lists the offending `_id`s, capped) plus a link that filters the grid to
 * the offending rows (`violationLink`): row `_id`s are not filterable, so it
 * filters on the column instead (required: `is_null`; a removed option:
 * `eq`, or `contains` on a multi column; unique: the non-empty cells sorted
 * by the column so duplicates sit together). Other errors with `op_index`
 * show at the operation's column, the rest in a banner.
 *
 * Unsaved changes: while the draft has operations, useUnsavedChangesGuard
 * holds same-origin link clicks and tab closes. A link to this same table
 * (the rows_violate filter links) navigates without asking (a normal
 * history entry) -- the panel and its draft stay; any other link, the
 * panel's own Close, Escape inside the panel, the footer's Discard and the
 * viewer switching to another panel (`requestClose(then)` on the ref
 * handle, TableSchemaEditorHandle) ask through DocConfirmDialog whether to
 * discard the edits. Discarding while the conflict banner is up takes the
 * newer schema and clears the banner.
 *
 * Phones (TableSchemaEditor.css, the 768px breakpoint of useIsMobile): the
 * panel is a full-screen sheet over the viewer -- its header with the close
 * button on top, the body scrolling, the Save / Discard footer pinned at the
 * bottom -- instead of a column beside the grid. It covers everything, so
 * there it is a modal dialog for screen readers (`role="dialog"
 * aria-modal="true"`, named by its heading) rather than a complementary
 * landmark.
 */

import {
  useCallback,
  useEffect,
  useId,
  useImperativeHandle,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
  type ReactNode,
  type Ref,
  type RefObject,
} from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { AlertCircle, ArrowDown, ArrowUp, ChevronDown, ChevronRight, Plus, Trash2, X } from 'lucide-react';
import {
  isStaleSchemaError,
  rowsViolation,
  staleSchemaCurrent,
  tableErrorDetail,
  updateTableSchema,
  type TableRowsViolation,
} from '../../api/tablesApi';
import type { FilterNode, SortKey, TableColumn, TableColumnType, TableDetail } from '../../api/types';
import { useIsMobile } from '../../hooks/useIsMobile';
import { useUnsavedChangesGuard } from '../../hooks/useUnsavedChangesGuard';
import { filterToSearchParam, sortToSearchParam } from '../../utils/tableFilter';
import {
  COLUMN_TYPES,
  NUMBER_MAX_PRECISION,
  TABLE_MAX_COLUMNS,
  TABLE_SCHEMA_MAX_OPERATIONS,
  UNIQUE_COLUMN_TYPES,
  deriveSchemaChange,
  draftFromSchema,
  newDraftColumn,
  newDraftId,
  rebaseDraft,
  schemaChangePhrase,
  settleReaddedOptions,
  summarizeSchemaChange,
  validateDraft,
  type ColumnErrors,
  type DraftColumn,
  type DraftErrors,
  type DraftOption,
  type SchemaDraft,
} from '../../utils/tableSchemaOps';
import { COLUMN_TYPE_LABELS, columnTypeLabel, tableValueText } from '../../utils/tableCells';
import { tableViewerPath } from '../../utils/tablesRoute';
import { DocConfirmDialog } from '../docs/DocConfirmDialog';
import './TableSchemaEditor.css';

export const TYPE_FIXED_NOTE = "Type can't be changed after creation";
export const MULTI_FIXED_NOTE = "Can't switch back to a single option";
export const STALE_SCHEMA_BANNER = "This table's columns changed while you were editing.";
export const HAS_PROBLEMS = 'Has problems';
export const COLUMNS_FULL_NOTE = `A table can have at most ${TABLE_MAX_COLUMNS} columns.`;
const OWNER_ONLY = 'Only the owner can change columns.';

/** "N problems to fix" (the hosts' reveal buttons). */
export function problemsPhrase(n: number): string {
  return `${n} ${n === 1 ? 'problem' : 'problems'} to fix`;
}

function columnLabel(column: { name: string }): string {
  return column.name.trim() || 'untitled column';
}

/** The live-region line after a move (`name` as shown: never re-cased). */
function movedPhrase(name: string, index: number, count: number): string {
  return `${name} moved to position ${index + 1} of ${count}`;
}

type MoveDirection = 'up' | 'down';

/**
 * Keyboard focus across a Move up / Move down: React re-inserts a moved
 * list item, which drops focus to the page in Chrome, and at either end the
 * pressed button turns disabled. `buttonRef(id, dir)` registers each move
 * button; after `moved(id, dir)` a layout effect focuses that button again,
 * or the opposite one when it is disabled.
 */
function useMoveFocus() {
  const buttons = useRef(new Map<string, HTMLButtonElement>());
  const [lastMove, setLastMove] = useState<{ id: string; dir: MoveDirection } | null>(null);
  useLayoutEffect(() => {
    if (!lastMove) return;
    const same = buttons.current.get(`${lastMove.id}:${lastMove.dir}`);
    const other = buttons.current.get(`${lastMove.id}:${lastMove.dir === 'up' ? 'down' : 'up'}`);
    const target = same && !same.disabled ? same : other && !other.disabled ? other : null;
    target?.focus();
  }, [lastMove]);
  const buttonRef = useCallback(
    (id: string, dir: MoveDirection) => (el: HTMLButtonElement | null) => {
      const key = `${id}:${dir}`;
      if (el) buttons.current.set(key, el);
      else buttons.current.delete(key);
    },
    [],
  );
  const moved = useCallback((id: string, dir: MoveDirection) => setLastMove({ id, dir }), []);
  return { buttonRef, moved };
}

// --- Editor body -------------------------------------------------------------------

/** What a host can ask of the editor body. */
export interface SchemaEditorBodyHandle {
  /** Expand every card with a problem and focus the first problem's field. */
  revealProblems: () => void;
}

export interface SchemaEditorBodyProps {
  draft: SchemaDraft;
  onChange: (draft: SchemaDraft) => void;
  /** The loaded schema the draft edits ([] for a new table). */
  base: TableColumn[];
  errors: DraftErrors;
  /** Server messages per draft column id (an operation's op_index, a spec error). */
  serverErrors?: Record<string, string>;
  disabled?: boolean;
  /**
   * Expanded cards by draft column id, when the host keeps them (so they
   * survive a re-based draft); else the body does (new columns open).
   */
  expanded?: ReadonlySet<string>;
  onExpandedChange?: (next: Set<string>) => void;
  ref?: Ref<SchemaEditorBodyHandle>;
}

/** Switch a NEW column to another type, dropping what the new type cannot hold. */
function withType(column: DraftColumn, type: TableColumnType): DraftColumn {
  return {
    ...column,
    type,
    unique: UNIQUE_COLUMN_TYPES.includes(type) ? column.unique : false,
    multiple: type === 'enum' ? column.multiple : false,
    precision: type === 'number' ? column.precision : '',
    defaultValue: '',
    defaultOptionIds: [],
    options:
      type === 'enum'
        ? column.options.length > 0
          ? column.options
          : [{ id: newDraftId('opt'), original: null, value: '' }]
        : [],
  };
}

export function SchemaEditorBody({
  draft,
  onChange,
  base,
  errors,
  serverErrors = {},
  disabled = false,
  expanded: hostExpanded,
  onExpandedChange,
  ref,
}: SchemaEditorBodyProps) {
  const idPrefix = useId();
  const baseByKey = useMemo(() => new Map(base.map((c) => [c.key, c])), [base]);
  // Expanded cards (unless the host keeps them): new columns open, loaded
  // ones closed.
  const [ownExpanded, setOwnExpanded] = useState<Set<string>>(
    () => new Set(draft.columns.filter((c) => c.key === null).map((c) => c.id)),
  );
  const expanded: ReadonlySet<string> = hostExpanded ?? ownExpanded;
  const setExpanded = (next: Set<string>) => {
    if (onExpandedChange) onExpandedChange(next);
    else setOwnExpanded(next);
  };
  const [announcement, setAnnouncement] = useState('');
  const { buttonRef, moved } = useMoveFocus();
  const cards = useRef(new Map<string, HTMLLIElement>());
  const addButtonRef = useRef<HTMLButtonElement>(null);
  // revealProblems: the card whose first problem gets focus after render.
  const [reveal, setReveal] = useState<{ id: string } | null>(null);
  // A deleted column's place: its successor's toggle gets focus after render.
  const [removedAt, setRemovedAt] = useState<{ index: number } | null>(null);

  const columns = draft.columns;
  const full = columns.length >= TABLE_MAX_COLUMNS;
  const setColumns = (next: DraftColumn[]) => onChange({ ...draft, columns: next });
  const update = (id: string, patch: Partial<DraftColumn>) =>
    setColumns(columns.map((c) => (c.id === id ? { ...c, ...patch } : c)));
  const move = (index: number, delta: number) => {
    const target = index + delta;
    if (target < 0 || target >= columns.length) return;
    const column = columns[index];
    const next = [...columns];
    [next[index], next[target]] = [next[target], next[index]];
    setColumns(next);
    moved(column.id, delta < 0 ? 'up' : 'down');
    setAnnouncement(movedPhrase(column.name.trim() || 'Untitled column', target, columns.length));
  };
  const remove = (id: string) => {
    const index = columns.findIndex((c) => c.id === id);
    if (index === -1) return;
    setColumns(columns.filter((c) => c.id !== id));
    setRemovedAt({ index });
    setAnnouncement(`${columns[index].name.trim() || 'Untitled column'} deleted`);
  };
  const add = () => {
    if (full) return;
    const column = newDraftColumn('', 'text');
    setColumns([...columns, column]);
    setExpanded(new Set(expanded).add(column.id));
  };
  const toggle = (id: string) => {
    const next = new Set(expanded);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    setExpanded(next);
  };

  useImperativeHandle(ref, () => ({
    revealProblems: () => {
      const ids = columns.filter((c) => errors.columns[c.id] || serverErrors[c.id]).map((c) => c.id);
      if (ids.length === 0) return;
      setExpanded(new Set([...expanded, ...ids]));
      setReveal({ id: ids[0] });
    },
  }));
  useLayoutEffect(() => {
    if (!removedAt) return;
    const next = columns[Math.min(removedAt.index, columns.length - 1)];
    const toggle = next ? cards.current.get(next.id)?.querySelector<HTMLElement>('.schema-editor-column-toggle') : null;
    (toggle ?? addButtonRef.current)?.focus();
    // Only the render that removed the column moves focus.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [removedAt]);
  useLayoutEffect(() => {
    if (!reveal) return;
    const card = cards.current.get(reveal.id);
    const field =
      card?.querySelector<HTMLElement>('[aria-invalid="true"]') ??
      card?.querySelector<HTMLElement>('.schema-editor-column-toggle');
    field?.focus();
  }, [reveal]);

  return (
    <div className="schema-editor">
      {errors.form && (
        <div className="schema-editor-error" role="alert">
          {errors.form}
        </div>
      )}
      <ol className="schema-editor-columns">
        {columns.map((column, index) => {
          const columnErrors = errors.columns[column.id];
          const serverError = serverErrors[column.id];
          const isOpen = expanded.has(column.id) || Boolean(serverError);
          const label = columnLabel(column);
          const hasProblem = Boolean(columnErrors) || Boolean(serverError);
          // A collapsed card's problems are out of sight: say so in words.
          const problemsId = !isOpen && columnErrors ? `${idPrefix}-${column.id}-problems` : undefined;
          return (
            <li
              key={column.id}
              ref={(el) => {
                if (el) cards.current.set(column.id, el);
                else cards.current.delete(column.id);
              }}
              className={`schema-editor-column${hasProblem ? ' schema-editor-column--invalid' : ''}`}
            >
              <div className="schema-editor-column-head">
                <button
                  type="button"
                  className="schema-editor-column-toggle"
                  onClick={() => toggle(column.id)}
                  aria-expanded={isOpen}
                  aria-label={`${isOpen ? 'Collapse' : 'Expand'} ${label}`}
                  aria-describedby={problemsId}
                >
                  {isOpen ? <ChevronDown size={14} aria-hidden="true" /> : <ChevronRight size={14} aria-hidden="true" />}
                  <span className="schema-editor-column-name">{column.name.trim() || 'Untitled column'}</span>
                  <span className="schema-editor-column-type">{columnSummary(column)}</span>
                </button>
                <button
                  type="button"
                  ref={buttonRef(column.id, 'up')}
                  className="schema-editor-icon-button"
                  onClick={() => move(index, -1)}
                  disabled={disabled || index === 0}
                  aria-label={`Move ${label} up`}
                  title="Move up"
                >
                  <ArrowUp size={14} aria-hidden="true" />
                </button>
                <button
                  type="button"
                  ref={buttonRef(column.id, 'down')}
                  className="schema-editor-icon-button"
                  onClick={() => move(index, 1)}
                  disabled={disabled || index === columns.length - 1}
                  aria-label={`Move ${label} down`}
                  title="Move down"
                >
                  <ArrowDown size={14} aria-hidden="true" />
                </button>
                <button
                  type="button"
                  className="schema-editor-icon-button schema-editor-icon-button--danger"
                  onClick={() => remove(column.id)}
                  disabled={disabled || columns.length === 1}
                  aria-label={`Delete ${label}`}
                  title={columns.length === 1 ? 'A table needs at least one column' : 'Delete column'}
                >
                  <Trash2 size={14} aria-hidden="true" />
                </button>
              </div>
              {problemsId && (
                <div id={problemsId} className="schema-editor-column-problems">
                  <AlertCircle size={14} aria-hidden="true" />
                  {HAS_PROBLEMS}
                </div>
              )}
              {serverError && (
                <div className="schema-editor-error" role="alert">
                  {serverError}
                </div>
              )}
              {isOpen && (
                <ColumnForm
                  idBase={`${idPrefix}-${column.id}`}
                  column={column}
                  loaded={column.key !== null ? baseByKey.get(column.key) ?? null : null}
                  errors={columnErrors}
                  disabled={disabled}
                  onChange={(patch) => update(column.id, patch)}
                  onAnnounce={setAnnouncement}
                />
              )}
            </li>
          );
        })}
      </ol>
      <div className="schema-editor-add-row">
        <button
          type="button"
          ref={addButtonRef}
          className="schema-editor-add"
          onClick={add}
          disabled={disabled || full}
          title={full ? COLUMNS_FULL_NOTE : undefined}
          aria-describedby={full ? `${idPrefix}-full` : undefined}
        >
          <Plus size={14} aria-hidden="true" />
          Add column
        </button>
        {full && (
          <span id={`${idPrefix}-full`} className="schema-editor-note">
            {COLUMNS_FULL_NOTE}
          </span>
        )}
      </div>
      <div className="schema-editor-sr-only" role="status" aria-live="polite">
        {announcement}
      </div>
    </div>
  );
}

/** A card's collapsed summary: the type (columnTypeLabel; a precision only once it is valid), flags. */
function columnSummary(column: DraftColumn): string {
  const precision = /^\d+$/.test(column.precision.trim()) ? Number(column.precision.trim()) : null;
  const parts = [
    columnTypeLabel({
      type: column.type,
      multiple: column.multiple,
      precision: precision !== null && precision <= NUMBER_MAX_PRECISION ? precision : null,
    }),
  ];
  if (column.required) parts.push('required');
  if (column.unique && UNIQUE_COLUMN_TYPES.includes(column.type)) parts.push('unique');
  return parts.join(' · ');
}

function FieldError({ id, message }: { id: string; message?: string }) {
  if (!message) return null;
  return (
    <div id={id} className="schema-editor-field-error">
      {message}
    </div>
  );
}

function ColumnForm({
  idBase,
  column,
  loaded,
  errors,
  disabled,
  onChange,
  onAnnounce,
}: {
  idBase: string;
  column: DraftColumn;
  loaded: TableColumn | null;
  errors?: ColumnErrors;
  disabled: boolean;
  onChange: (patch: Partial<DraftColumn>) => void;
  /** A line for the body's live region (an option moved). */
  onAnnounce: (text: string) => void;
}) {
  const isNew = column.key === null;
  const multiFixed = Boolean(loaded?.multiple);
  const err = (field: keyof Omit<ColumnErrors, 'optionErrors'>) => errors?.[field];
  const describedBy = (field: string, message?: string) => (message ? `${idBase}-${field}-error` : undefined);

  return (
    <div className="schema-editor-form">
      <label className="schema-editor-label" htmlFor={`${idBase}-name`}>
        Name
      </label>
      <input
        id={`${idBase}-name`}
        className="schema-editor-input"
        value={column.name}
        onChange={(e) => onChange({ name: e.target.value })}
        disabled={disabled}
        aria-invalid={Boolean(err('name'))}
        aria-describedby={describedBy('name', err('name'))}
      />
      <FieldError id={`${idBase}-name-error`} message={err('name')} />

      <label className="schema-editor-label" htmlFor={`${idBase}-type`}>
        Type
      </label>
      <select
        id={`${idBase}-type`}
        className="schema-editor-input"
        value={column.type}
        onChange={(e) => onChange(withType(column, e.target.value as TableColumnType))}
        disabled={disabled || !isNew}
        aria-describedby={!isNew ? `${idBase}-type-note` : undefined}
      >
        {COLUMN_TYPES.map((type) => (
          <option key={type} value={type}>
            {COLUMN_TYPE_LABELS[type]}
          </option>
        ))}
      </select>
      {!isNew && (
        <div id={`${idBase}-type-note`} className="schema-editor-note">
          {TYPE_FIXED_NOTE}
        </div>
      )}
      <FieldError id={`${idBase}-type-error`} message={err('type')} />

      <label className="schema-editor-label" htmlFor={`${idBase}-description`}>
        Description <span className="schema-editor-optional">(optional)</span>
      </label>
      <textarea
        id={`${idBase}-description`}
        className="schema-editor-input schema-editor-textarea"
        value={column.description}
        rows={2}
        onChange={(e) => onChange({ description: e.target.value })}
        disabled={disabled}
        aria-invalid={Boolean(err('description'))}
        aria-describedby={describedBy('description', err('description'))}
      />
      <FieldError id={`${idBase}-description-error`} message={err('description')} />

      <div className="schema-editor-checks">
        <label className="schema-editor-check">
          <input
            type="checkbox"
            checked={column.required}
            onChange={(e) => onChange({ required: e.target.checked })}
            disabled={disabled}
          />
          Required
        </label>
        {UNIQUE_COLUMN_TYPES.includes(column.type) && (
          <label className="schema-editor-check">
            <input
              type="checkbox"
              checked={column.unique}
              onChange={(e) => onChange({ unique: e.target.checked })}
              disabled={disabled}
            />
            Unique
          </label>
        )}
        {column.type === 'enum' && (
          <label className="schema-editor-check">
            <input
              type="checkbox"
              checked={column.multiple}
              onChange={(e) =>
                onChange({
                  multiple: e.target.checked,
                  defaultOptionIds: e.target.checked ? column.defaultOptionIds : column.defaultOptionIds.slice(0, 1),
                })
              }
              disabled={disabled || multiFixed}
              aria-describedby={multiFixed ? `${idBase}-multi-note` : undefined}
            />
            Allow several options
          </label>
        )}
      </div>
      {column.type === 'enum' && multiFixed && (
        <div id={`${idBase}-multi-note`} className="schema-editor-note">
          {MULTI_FIXED_NOTE}
        </div>
      )}
      <FieldError id={`${idBase}-unique-error`} message={err('unique')} />
      <FieldError id={`${idBase}-multiple-error`} message={err('multiple')} />

      {column.type === 'number' && (
        <>
          <label className="schema-editor-label" htmlFor={`${idBase}-precision`}>
            Decimal places <span className="schema-editor-optional">(0 to {NUMBER_MAX_PRECISION}, optional)</span>
          </label>
          <input
            id={`${idBase}-precision`}
            className="schema-editor-input schema-editor-input--short"
            inputMode="numeric"
            value={column.precision}
            placeholder="Any"
            onChange={(e) => onChange({ precision: e.target.value })}
            disabled={disabled}
            aria-invalid={Boolean(err('precision'))}
            aria-describedby={describedBy('precision', err('precision'))}
          />
          <FieldError id={`${idBase}-precision-error`} message={err('precision')} />
        </>
      )}

      {column.type === 'enum' && (
        <OptionsEditor
          idBase={idBase}
          column={column}
          loaded={loaded}
          errors={errors}
          disabled={disabled}
          onChange={onChange}
          onAnnounce={onAnnounce}
        />
      )}

      <DefaultInput idBase={idBase} column={column} error={err('default')} disabled={disabled} onChange={onChange} />
    </div>
  );
}

function OptionsEditor({
  idBase,
  column,
  loaded,
  errors,
  disabled,
  onChange,
  onAnnounce,
}: {
  idBase: string;
  column: DraftColumn;
  loaded: TableColumn | null;
  errors?: ColumnErrors;
  disabled: boolean;
  onChange: (patch: Partial<DraftColumn>) => void;
  onAnnounce: (text: string) => void;
}) {
  const isNew = column.key === null;
  const options = column.options;
  const { buttonRef, moved } = useMoveFocus();
  const inputs = useRef(new Map<string, HTMLInputElement>());
  const addOptionRef = useRef<HTMLButtonElement>(null);
  // A removed option's place: the input that takes it gets focus after render.
  const [removedAt, setRemovedAt] = useState<{ index: number } | null>(null);
  useLayoutEffect(() => {
    if (!removedAt) return;
    const next = options[Math.min(removedAt.index, options.length - 1)];
    const input = next ? inputs.current.get(next.id) : undefined;
    (input ?? addOptionRef.current)?.focus();
    // Only the render that removed the option moves focus.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [removedAt]);
  const setOptions = (next: DraftOption[]) => onChange({ options: next });
  const removeOption = (index: number) => {
    const option = options[index];
    setOptions(options.filter((o) => o.id !== option.id));
    setRemovedAt({ index });
    onAnnounce(`Option ${option.value.trim() || index + 1} removed`);
  };
  const optionLabel = (option: DraftOption, index: number) => option.value.trim() || `option ${index + 1}`;
  const moveOption = (index: number, delta: number) => {
    const target = index + delta;
    if (target < 0 || target >= options.length) return;
    const option = options[index];
    const next = [...options];
    [next[index], next[target]] = [next[target], next[index]];
    setOptions(next);
    moved(option.id, delta < 0 ? 'up' : 'down');
    onAnnounce(movedPhrase(option.value.trim() || `Option ${index + 1}`, target, options.length));
  };
  // A loaded column: an option re-typed after its removal goes back to its
  // place (and stays the same option) once its field is left.
  const settle = () => {
    if (isNew) return;
    const settled = settleReaddedOptions(column, loaded);
    if (settled !== column) setOptions(settled.options);
  };
  return (
    <fieldset className="schema-editor-options">
      <legend className="schema-editor-label">Options</legend>
      <ul className="schema-editor-option-list">
        {options.map((option, index) => {
          const usedByDefault = column.defaultOptionIds.includes(option.id);
          const last = options.length === 1;
          const label = optionLabel(option, index);
          const message = errors?.optionErrors[option.id];
          return (
            <li key={option.id} className="schema-editor-option">
              <div className="schema-editor-option-row">
                <input
                  ref={(el) => {
                    if (el) inputs.current.set(option.id, el);
                    else inputs.current.delete(option.id);
                  }}
                  className="schema-editor-input"
                  value={option.value}
                  aria-label={`Option ${index + 1}`}
                  onChange={(e) =>
                    setOptions(options.map((o) => (o.id === option.id ? { ...o, value: e.target.value } : o)))
                  }
                  onBlur={settle}
                  disabled={disabled}
                  aria-invalid={Boolean(message)}
                  aria-describedby={message ? `${idBase}-${option.id}-error` : undefined}
                />
                {isNew && (
                  <>
                    <button
                      type="button"
                      ref={buttonRef(option.id, 'up')}
                      className="schema-editor-icon-button"
                      onClick={() => moveOption(index, -1)}
                      disabled={disabled || index === 0}
                      aria-label={`Move ${label} up`}
                    >
                      <ArrowUp size={14} aria-hidden="true" />
                    </button>
                    <button
                      type="button"
                      ref={buttonRef(option.id, 'down')}
                      className="schema-editor-icon-button"
                      onClick={() => moveOption(index, 1)}
                      disabled={disabled || index === options.length - 1}
                      aria-label={`Move ${label} down`}
                    >
                      <ArrowDown size={14} aria-hidden="true" />
                    </button>
                  </>
                )}
                <button
                  type="button"
                  className="schema-editor-icon-button schema-editor-icon-button--danger"
                  onClick={() => removeOption(index)}
                  disabled={disabled || last || usedByDefault}
                  aria-label={`Remove option ${label}`}
                  title={
                    last
                      ? 'A choice column needs at least one option'
                      : usedByDefault
                        ? 'Used as the default; change the default first'
                        : 'Remove option'
                  }
                >
                  <X size={14} aria-hidden="true" />
                </button>
              </div>
              <FieldError id={`${idBase}-${option.id}-error`} message={message} />
            </li>
          );
        })}
      </ul>
      <FieldError id={`${idBase}-options-error`} message={errors?.options} />
      <button
        type="button"
        ref={addOptionRef}
        className="schema-editor-add schema-editor-add--small"
        onClick={() => setOptions([...options, { id: newDraftId('opt'), original: null, value: '' }])}
        disabled={disabled}
      >
        <Plus size={14} aria-hidden="true" />
        Add option
      </button>
    </fieldset>
  );
}

function DefaultInput({
  idBase,
  column,
  error,
  disabled,
  onChange,
}: {
  idBase: string;
  column: DraftColumn;
  error?: string;
  disabled: boolean;
  onChange: (patch: Partial<DraftColumn>) => void;
}) {
  const id = `${idBase}-default`;
  const errorId = `${id}-error`;
  const common = {
    disabled,
    'aria-invalid': Boolean(error),
    'aria-describedby': error ? errorId : undefined,
  };
  let input: ReactNode;
  if (column.type === 'boolean') {
    input = (
      <select id={id} className="schema-editor-input" value={column.defaultValue} onChange={(e) => onChange({ defaultValue: e.target.value })} {...common}>
        <option value="">No default</option>
        <option value="true">{tableValueText(true)}</option>
        <option value="false">{tableValueText(false)}</option>
      </select>
    );
  } else if (column.type === 'enum' && !column.multiple) {
    input = (
      <select
        id={id}
        className="schema-editor-input"
        value={column.defaultOptionIds[0] ?? ''}
        onChange={(e) => onChange({ defaultOptionIds: e.target.value ? [e.target.value] : [] })}
        {...common}
      >
        <option value="">No default</option>
        {column.options.map((option, index) => (
          <option key={option.id} value={option.id}>
            {option.value || `Option ${index + 1}`}
          </option>
        ))}
      </select>
    );
  } else if (column.type === 'enum') {
    return (
      <fieldset className="schema-editor-options">
        <legend className="schema-editor-label">
          Default <span className="schema-editor-optional">(optional)</span>
        </legend>
        <div className="schema-editor-checks">
          {column.options.map((option, index) => (
            <label key={option.id} className="schema-editor-check">
              <input
                type="checkbox"
                checked={column.defaultOptionIds.includes(option.id)}
                onChange={(e) =>
                  onChange({
                    defaultOptionIds: e.target.checked
                      ? [...column.defaultOptionIds, option.id]
                      : column.defaultOptionIds.filter((x) => x !== option.id),
                  })
                }
                disabled={disabled}
              />
              {option.value || `Option ${index + 1}`}
            </label>
          ))}
        </div>
        <FieldError id={errorId} message={error} />
      </fieldset>
    );
  } else {
    input = (
      <input
        id={id}
        className="schema-editor-input"
        value={column.defaultValue}
        inputMode={column.type === 'number' ? 'decimal' : column.type === 'url' ? 'url' : undefined}
        placeholder={column.type === 'url' ? 'https://' : 'No default'}
        onChange={(e) => onChange({ defaultValue: e.target.value })}
        {...common}
      />
    );
  }
  return (
    <>
      <label className="schema-editor-label" htmlFor={id}>
        Default <span className="schema-editor-optional">(optional)</span>
      </label>
      {input}
      <FieldError id={errorId} message={error} />
    </>
  );
}

// --- Read-only view ----------------------------------------------------------------

/**
 * The columns and their settings as text (viewers who cannot edit); a
 * default reads as the cards and History write it (`tableValueText`:
 * booleans true / false).
 */
export function ColumnsReadOnly({ columns }: { columns: TableColumn[] }) {
  return (
    <ol className="schema-editor-columns schema-editor-readonly">
      {columns.map((column) => {
        const flags = [
          columnTypeLabel(column),
          column.required ? 'required' : null,
          column.unique ? 'unique' : null,
        ].filter(Boolean);
        const fallback = tableValueText(column.default);
        return (
          <li key={column.key} className="schema-editor-column">
            <div className="schema-editor-readonly-name">{column.name}</div>
            <div className="schema-editor-column-type">{flags.join(' · ')}</div>
            {column.type === 'enum' && column.options && (
              <div className="schema-editor-readonly-line">Options: {column.options.join(', ')}</div>
            )}
            {fallback !== null && <div className="schema-editor-readonly-line">Default: {fallback}</div>}
            {column.description && <div className="schema-editor-readonly-line">{column.description}</div>}
          </li>
        );
      })}
    </ol>
  );
}

// --- rows_violate links --------------------------------------------------------------

export interface ViolationLink {
  label: string;
  to: string;
}

/**
 * The grid link for a rows_violate refusal, or null. The server names the
 * column as it is in the NEW schema; the grid still shows the loaded one,
 * so the column is found in the draft by that name and addressed by its
 * loaded name (a column the draft adds has no rows to show; an option the
 * loaded column lacks -- a temporary spelling -- gets no link). Row `_id` is
 * not filterable, so the link filters on the column: `required` -> the
 * empty cells; `enum_option` -> the cells holding the option (`contains`
 * on a multi column); `unique` -> the non-empty cells sorted by the column.
 */
export function violationLink(
  violation: TableRowsViolation,
  draft: SchemaDraft,
  base: TableColumn[],
  tableId: string,
): ViolationLink | null {
  if (!violation.column) return null;
  const drafted = draft.columns.find((c) => c.name === violation.column);
  const loaded = drafted?.key ? base.find((c) => c.key === drafted.key) : undefined;
  if (!loaded) return null;
  let filter: FilterNode;
  let sort: SortKey[] | null = null;
  let label: string;
  if (violation.check === 'required') {
    filter = { column: loaded.name, op: 'is_null' };
    label = `Show the rows with no ${loaded.name}`;
  } else if (
    violation.check === 'enum_option' &&
    violation.option !== null &&
    (loaded.options ?? []).includes(violation.option)
  ) {
    filter = loaded.multiple
      ? { column: loaded.name, op: 'contains', value: violation.option }
      : { column: loaded.name, op: 'eq', value: violation.option };
    label = `Show the rows using ${violation.option}`;
  } else if (violation.check === 'unique') {
    filter = { column: loaded.name, op: 'not_null' };
    sort = [{ column: loaded.name, direction: 'asc' }];
    label = `Show the rows sorted by ${loaded.name}`;
  } else {
    return null;
  }
  return {
    label,
    to: tableViewerPath(tableId, { filter: filterToSearchParam(filter), sort: sortToSearchParam(sort) }),
  };
}

// --- The Columns panel --------------------------------------------------------------

interface Base {
  version: number;
  title: string;
  columns: TableColumn[];
}

interface Conflict extends Base {
  /** Raised by a version move while editing, not by a refused save. */
  proactive: boolean;
}

interface Violation {
  /** The server's message; it already lists the offending `_id`s (capped). */
  message: string;
  link: ViolationLink | null;
}

/** What runs once the user confirms discarding the edits. */
type PendingDiscard =
  | { kind: 'close'; run: () => void }
  | { kind: 'nav'; to: string }
  | { kind: 'discard' };

/** What the viewer can ask of an open Columns panel. */
export interface TableSchemaEditorHandle {
  /**
   * Leave the panel: run `then` (default: `onClose`) at once without edits,
   * else after the discard confirm (nothing runs when the user keeps the
   * edits). The viewer uses it to switch to another panel.
   */
  requestClose: (then?: () => void) => void;
}

export interface TableSchemaEditorProps {
  table: TableDetail;
  /** Close the panel (after any discard confirm). */
  onClose: () => void;
  /** A detail returned by PUT /schema (useTable.applyDetail). */
  onDetailApplied: (detail: TableDetail) => void;
  /** Re-fetch the table (useTable.refresh). */
  onRefresh: () => void;
  ref?: Ref<TableSchemaEditorHandle>;
}

function baseOf(table: TableDetail): Base {
  return { version: table.schema_version, title: table.title, columns: table.schema };
}

function errorMessage(err: unknown, fallback: string): string {
  return err instanceof Error && err.message ? err.message : fallback;
}

export function TableSchemaEditor({ table, onClose, onDetailApplied, onRefresh, ref }: TableSchemaEditorProps) {
  const canEdit = table.access.can_edit_schema;
  const isMobile = useIsMobile();
  const headingId = useId();
  const headingRef = useRef<HTMLHeadingElement>(null);
  // The editor's own handle (its draft decides whether to confirm); the
  // read-only view has nothing to lose.
  const editorRef = useRef<TableSchemaEditorHandle>(null);
  const requestClose = useCallback(
    (then?: () => void) => {
      if (editorRef.current) editorRef.current.requestClose(then);
      else (then ?? onClose)();
    },
    [onClose],
  );
  useImperativeHandle(ref, () => ({ requestClose }), [requestClose]);
  // Escape from inside the panel closes it (through the discard confirm when
  // there are edits). The portaled confirm's key events bubble through here
  // too: only an event whose target is inside the panel's own DOM counts.
  // The key is consumed here: the confirm it opens mounts during this
  // dispatch and adds ModalShell's document Escape listener, which the same
  // event would otherwise reach and close the confirm at once.
  const handleKeyDown = (e: KeyboardEvent<HTMLElement>) => {
    // An IME composition's Escape only cancels the composition.
    if (e.key !== 'Escape' || e.defaultPrevented || e.nativeEvent.isComposing) return;
    if (!(e.target instanceof Node) || !e.currentTarget.contains(e.target)) return;
    e.preventDefault();
    e.stopPropagation();
    requestClose();
  };
  // Phones: a full-screen sheet, so a modal dialog; a landmark beside the
  // grid on wider screens. One element type either way, so crossing the
  // breakpoint never remounts the panel (and its draft).
  return (
    <div
      className="table-side-panel schema-editor-panel"
      role={isMobile ? 'dialog' : 'complementary'}
      aria-modal={isMobile ? true : undefined}
      aria-labelledby={headingId}
      onKeyDown={handleKeyDown}
    >
      {canEdit ? (
        <ColumnsPanelEditor
          ref={editorRef}
          table={table}
          headingId={headingId}
          headingRef={headingRef}
          onClose={onClose}
          onDetailApplied={onDetailApplied}
          onRefresh={onRefresh}
        />
      ) : (
        <>
          <PanelHeader headingId={headingId} headingRef={headingRef} onClose={onClose} />
          <div className="table-side-panel-body">
            <p className="schema-editor-note schema-editor-deny">
              {table.access.schema_deny_reason || OWNER_ONLY}
            </p>
            <ColumnsReadOnly columns={table.schema} />
          </div>
        </>
      )}
    </div>
  );
}

function PanelHeader({
  headingId,
  headingRef,
  onClose,
}: {
  headingId: string;
  headingRef: RefObject<HTMLHeadingElement | null>;
  onClose: () => void;
}) {
  // Opened from the header menu: focus lands on the heading, as History's.
  useEffect(() => {
    headingRef.current?.focus();
  }, [headingRef]);
  return (
    <div className="table-side-panel-header">
      <h3 id={headingId} ref={headingRef} tabIndex={-1} className="table-side-panel-title">
        Columns
      </h3>
      <button type="button" className="schema-editor-icon-button" onClick={onClose} aria-label="Close columns panel">
        <X size={16} aria-hidden="true" />
      </button>
    </div>
  );
}

function ColumnsPanelEditor({
  table,
  headingId,
  headingRef,
  onClose,
  onDetailApplied,
  onRefresh,
  ref,
}: TableSchemaEditorProps & { headingId: string; headingRef: RefObject<HTMLHeadingElement | null> }) {
  const navigate = useNavigate();
  const [base, setBase] = useState<Base>(() => baseOf(table));
  const [draft, setDraft] = useState<SchemaDraft>(() => draftFromSchema(table.title, table.schema));
  // Expanded cards, by draft column id: kept here so a re-base (a save, a
  // conflict reload) leaves them as they were.
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set());
  const bodyRef = useRef<SchemaEditorBodyHandle>(null);
  const [saving, setSaving] = useState(false);
  const [conflict, setConflict] = useState<Conflict | null>(null);
  const [violation, setViolation] = useState<Violation | null>(null);
  const [banner, setBanner] = useState<string | null>(null);
  const [serverErrors, setServerErrors] = useState<Record<string, string>>({});
  const [notes, setNotes] = useState<string[]>([]);
  const [saved, setSaved] = useState<string | null>(null);
  // The discard confirm: closing the panel (or opening another one), the
  // link the user clicked, or the footer's Discard.
  const [pending, setPending] = useState<PendingDiscard | null>(null);
  // Bumped after an action that may remove the focused control (Save turns
  // disabled, a banner or Discard goes away): focus then moves to the
  // heading instead of falling to the page.
  const [focusCheck, setFocusCheck] = useState(0);

  const derived = useMemo(() => deriveSchemaChange(base.columns, base.title, draft), [base, draft]);
  const errors = useMemo(
    () => validateDraft(base.columns, draft, { tableEmpty: table.row_count === 0 }),
    [base.columns, draft, table.row_count],
  );
  const opCount = derived.operations.length;
  const dirty = opCount > 0;
  const phrase = schemaChangePhrase(summarizeSchemaChange(derived, draft));
  // chat/tables/schema_ops.py refuses longer lists (too_many_operations).
  const tooMany = opCount > TABLE_SCHEMA_MAX_OPERATIONS;

  useEffect(() => {
    if (focusCheck === 0) return;
    const active = document.activeElement;
    const lost =
      !active ||
      active === document.body ||
      !active.isConnected ||
      (active instanceof HTMLButtonElement && active.disabled);
    if (lost) headingRef.current?.focus();
  }, [focusCheck, headingRef]);
  const keepFocus = () => setFocusCheck((n) => n + 1);

  const clearFeedback = () => {
    setViolation(null);
    setBanner(null);
    setServerErrors({});
    setSaved(null);
  };

  const rebase = useCallback((next: Base, nextDraft?: SchemaDraft) => {
    setBase(next);
    setDraft(nextDraft ?? draftFromSchema(next.title, next.columns));
  }, []);

  const changeDraft = (next: SchemaDraft) => {
    setDraft(next);
    setSaved(null);
    setServerErrors({});
  };

  // The viewer's schema moved (someone else saved): re-base silently while
  // there are no edits, else raise the conflict banner. Never during our own
  // save (its response re-bases).
  const dirtyRef = useRef(dirty);
  dirtyRef.current = dirty;
  useEffect(() => {
    // Versions only grow: an older one is a re-fetch that lags our re-base.
    if (saving || table.schema_version <= base.version) return;
    if (conflict && conflict.version === table.schema_version) return;
    if (!dirtyRef.current) {
      rebase(baseOf(table));
      return;
    }
    setConflict({ ...baseOf(table), proactive: true });
    // `table` is read for its current schema; the version is the trigger.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [table.schema_version, base.version, saving]);

  const save = async () => {
    if (!dirty || tooMany || errors.count > 0 || saving || conflict) return;
    const { operations, targets } = derived;
    const savedPhrase = phrase;
    clearFeedback();
    setNotes([]);
    setSaving(true);
    try {
      const detail = await updateTableSchema(table.id, { operations, expected_schema_version: base.version });
      onDetailApplied(detail);
      // A new column's card follows it to its key (found by its name).
      const keyByName = new Map(detail.schema.map((c) => [c.name, c.key]));
      const newKeys = new Map(
        draft.columns.filter((c) => c.key === null).map((c) => [c.id, keyByName.get(c.name) ?? c.id]),
      );
      setExpanded((prev) => new Set([...prev].map((id) => newKeys.get(id) ?? id)));
      rebase({ version: detail.schema_version, title: detail.title, columns: detail.schema });
      setSaved(`Saved: ${savedPhrase}.`);
      keepFocus();
    } catch (err) {
      if (isStaleSchemaError(err)) {
        const current = staleSchemaCurrent(err);
        setConflict(
          current
            ? { version: current.schema_version, title: base.title, columns: current.schema, proactive: false }
            : { ...baseOf(table), proactive: false },
        );
        onRefresh();
        return;
      }
      const rows = rowsViolation(err);
      if (rows) {
        setViolation({
          message: errorMessage(err, 'Some rows break this change.'),
          link: violationLink(rows, draft, base.columns, table.id),
        });
        return;
      }
      const detail = tableErrorDetail(err);
      const opIndex = typeof detail?.op_index === 'number' ? detail.op_index : null;
      const target = opIndex !== null ? targets[opIndex] : null;
      if (target && draft.columns.some((c) => c.id === target)) {
        setServerErrors({ [target]: errorMessage(err, 'This change was refused.') });
      } else {
        setBanner(errorMessage(err, 'Failed to save the columns.'));
      }
    } finally {
      setSaving(false);
    }
  };

  const keepEdits = () => {
    if (!conflict) return;
    const result = rebaseDraft(base.columns, draft, conflict.title, conflict.columns, base.title);
    rebase(conflict, result.draft);
    setNotes(result.dropped);
    setConflict(null);
    clearFeedback();
    onRefresh();
    keepFocus();
  };
  // Throw the edits away: onto the newer schema while the conflict banner
  // is up (and clear it), else back to the base.
  const discardEdits = () => {
    if (conflict) {
      rebase(conflict);
      setConflict(null);
      onRefresh();
    } else {
      rebase(base);
    }
    setNotes([]);
    clearFeedback();
    keepFocus();
  };

  const viewerPath = tableViewerPath(table.id);
  useUnsavedChangesGuard({
    when: dirty,
    onBlockedNavigation: (to) => {
      // A link into this table's own grid (the rows_violate links) keeps
      // the panel and its draft: go without asking.
      if (to.split(/[?#]/)[0] === viewerPath) navigate(to);
      else setPending({ kind: 'nav', to });
    },
  });

  const requestClose = useCallback(
    (then?: () => void) => {
      const run = then ?? onClose;
      if (dirty) setPending({ kind: 'close', run });
      else run();
    },
    [dirty, onClose],
  );
  useImperativeHandle(ref, () => ({ requestClose }), [requestClose]);

  let summary: ReactNode;
  if (saved) summary = saved;
  else if (errors.count > 0) {
    summary = (
      <button type="button" className="schema-editor-problems" onClick={() => bodyRef.current?.revealProblems()}>
        {problemsPhrase(errors.count)}
      </button>
    );
  } else if (tooMany) {
    summary = `Too many changes for one save (${opCount} steps; at most ${TABLE_SCHEMA_MAX_OPERATIONS})`;
  } else summary = dirty ? phrase : 'No changes';

  return (
    <>
      <PanelHeader headingId={headingId} headingRef={headingRef} onClose={() => requestClose()} />
      <div className="table-side-panel-body">
        {conflict && (
          <div className="schema-editor-banner" role="alert">
            <p>{STALE_SCHEMA_BANNER}</p>
            <div className="schema-editor-banner-actions">
              <button type="button" className="schema-editor-button" onClick={keepEdits}>
                Reload and keep my edits
              </button>
              <button type="button" className="schema-editor-button" onClick={discardEdits}>
                Discard my edits
              </button>
            </div>
          </div>
        )}
        {notes.length > 0 && (
          <div className="schema-editor-banner" role="status">
            {notes.map((note) => (
              <p key={note}>{note}</p>
            ))}
          </div>
        )}
        {violation && (
          <div className="schema-editor-banner schema-editor-banner--error" role="alert">
            <p>{violation.message}</p>
            {violation.link && (
              <Link to={violation.link.to} className="schema-editor-link">
                {violation.link.label}
              </Link>
            )}
          </div>
        )}
        {banner && (
          <div className="schema-editor-banner schema-editor-banner--error" role="alert">
            <p>{banner}</p>
          </div>
        )}
        <SchemaEditorBody
          ref={bodyRef}
          draft={draft}
          onChange={changeDraft}
          base={base.columns}
          errors={errors}
          serverErrors={serverErrors}
          disabled={saving}
          expanded={expanded}
          onExpandedChange={setExpanded}
        />
      </div>
      <div className="table-side-panel-footer">
        <span className="schema-editor-summary" role="status">
          {summary}
        </span>
        {dirty && (
          <button
            type="button"
            className="schema-editor-button"
            onClick={() => setPending({ kind: 'discard' })}
            disabled={saving}
          >
            Discard
          </button>
        )}
        <button
          type="button"
          className="schema-editor-button schema-editor-button--primary"
          onClick={() => void save()}
          disabled={!dirty || tooMany || errors.count > 0 || saving || conflict !== null}
        >
          {saving ? 'Saving...' : 'Save'}
        </button>
      </div>

      <DocConfirmDialog
        isOpen={pending !== null}
        title="Discard column changes?"
        confirmLabel="Discard"
        tone="danger"
        busy={false}
        error={null}
        onConfirm={() => {
          const action = pending;
          setPending(null);
          discardEdits();
          if (action?.kind === 'nav') navigate(action.to);
          else if (action?.kind === 'close') action.run();
        }}
        onClose={() => setPending(null)}
      >
        <p>{`Your edits to the columns of "${table.title}" (${phrase.charAt(0).toLowerCase()}${phrase.slice(1)}) will be lost.`}</p>
      </DocConfirmDialog>
    </>
  );
}
