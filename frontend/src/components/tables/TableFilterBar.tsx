/**
 * TableFilterBar -- the table viewer's filter chips (/tables/<id>).
 *
 * Stateless about the filter itself: the viewer passes the route's parsed
 * `filter` and gets every change back through `onChange` (it navigates to the
 * new `?filter=`; the URL is the single source of truth). The bar turns the
 * filter into chips with utils/tableFilter (`filterToConditions`): one chip
 * per AND-ed condition, "Status is Customer", by column NAME. A filter the
 * bar cannot show as chips (or / not / nesting, an unknown column, an op or
 * value it does not offer) shows as one "Custom filter" chip with a Clear
 * button; a `filter` param that is not JSON shows a note with Clear.
 *
 * "Add filter" (or a click on a chip) opens a small dialog under the bar:
 * column -> condition (per column type) -> a typed value input, Apply
 * disabled while the condition is incomplete or invalid (`conditionError`).
 * Escape, Cancel or a click outside closes it and focus returns to the button
 * that opened it. "Clear all" removes every condition.
 *
 * A server refusal of the filter (`error`, from the rows query's
 * filterErrorPath and message) is shown on the bar; when its path points at
 * one condition (`filterErrorTarget`), that chip is marked invalid.
 */

import { useCallback, useEffect, useId, useMemo, useRef, useState } from 'react';
import type { FormEvent, KeyboardEvent as ReactKeyboardEvent, ReactNode } from 'react';
import { Plus, X } from 'lucide-react';
import type { FilterNode, FilterOp, TableColumn } from '../../api/types';
import {
  FILTER_MAX_CONDITIONS,
  conditionError,
  conditionLabel,
  conditionsToFilter,
  emptyValue,
  filterErrorTarget,
  filterToConditions,
  findColumn,
  missingFilterColumns,
  newConditionId,
  opsForColumn,
  splitLines,
  valueKind,
} from '../../utils/tableFilter';
import type { FilterCondition, FilterValue } from '../../utils/tableFilter';
import './TableFilterBar.css';

/** A server refusal of the filter: `filterErrorPath` and the message. */
export interface TableFilterError {
  path: string;
  message: string;
}

export interface TableFilterBarProps {
  /** The table's schema (columns offered by name). */
  columns: TableColumn[];
  /** The route's filter, parsed (null = none). */
  filter: FilterNode | null;
  /** The route has a `filter` param that is not a JSON object. */
  invalidParam?: boolean;
  /** Called with the new filter (null = cleared). */
  onChange: (next: FilterNode | null) => void;
  /** The server refused the filter, else null. */
  error?: TableFilterError | null;
}

interface EditState {
  /** The chip being edited, or null for a new condition. */
  index: number | null;
  draft: FilterCondition;
  /** The element focus returns to on close. */
  returnFocus: HTMLElement | null;
}

function newCondition(columns: TableColumn[]): FilterCondition {
  const column = columns[0];
  const op = column ? opsForColumn(column)[0].op : 'eq';
  return {
    id: newConditionId(),
    column: column?.name ?? '',
    op,
    value: column ? emptyValue(valueKind(column, op)) : null,
  };
}

export function TableFilterBar({
  columns,
  filter,
  invalidParam = false,
  onChange,
  error = null,
}: TableFilterBarProps) {
  const parsed = useMemo(() => filterToConditions(filter, columns), [filter, columns]);
  // Columns dropped or renamed since the link was made: the viewer does not
  // send such a filter (it would 400), the bar says why and offers Clear.
  const missing = useMemo(() => missingFilterColumns(filter, columns), [filter, columns]);
  const conditions = parsed.kind === 'conditions' ? parsed.conditions : [];
  const errorTarget = error ? filterErrorTarget(error.path, conditions.length) : null;
  const errorId = useId();

  const [edit, setEdit] = useState<EditState | null>(null);
  const editRef = useRef(edit);
  editRef.current = edit;
  const addButtonRef = useRef<HTMLButtonElement>(null);

  /** Focus `target` (else Add filter) once the dialog / chips re-rendered. */
  const refocus = useCallback((target: HTMLElement | null) => {
    setTimeout(() => {
      const el = target && target.isConnected ? target : addButtonRef.current;
      el?.focus();
    }, 0);
  }, []);

  const close = useCallback(() => {
    const target = editRef.current?.returnFocus ?? null;
    setEdit(null);
    refocus(target);
  }, [refocus]);

  const commit = useCallback(
    (next: FilterCondition[]) => {
      onChange(conditionsToFilter(next, columns));
    },
    [onChange, columns],
  );

  const apply = (draft: FilterCondition) => {
    if (!edit) return;
    const next =
      edit.index === null
        ? [...conditions, draft]
        : conditions.map((condition, index) => (index === edit.index ? draft : condition));
    setEdit(null);
    commit(next);
    // Chips re-render with new identities once the URL moves: focus Add.
    refocus(null);
  };

  const remove = (index: number) => {
    setEdit(null);
    commit(conditions.filter((_, i) => i !== index));
    refocus(null);
  };

  const clearAll = () => {
    setEdit(null);
    onChange(null);
    refocus(null);
  };

  const hasFilter = parsed.kind !== 'none' || invalidParam;
  const canAdd = parsed.kind !== 'custom' && !invalidParam && conditions.length < FILTER_MAX_CONDITIONS;
  const staleNote =
    missing.length === 1
      ? `This filter references a column that no longer exists (${missing[0]}).`
      : `This filter references columns that no longer exist (${missing.join(', ')}).`;
  const showError = error !== null && errorTarget !== null;

  return (
    <div className="table-filter-bar" role="group" aria-label="Filters">
      <div className="table-filter-row">
        {invalidParam ? (
          <span className="table-filter-note">This link&apos;s filter could not be read.</span>
        ) : missing.length > 0 ? (
          <span className="table-filter-note">{staleNote}</span>
        ) : parsed.kind === 'custom' ? (
          <span
            className={`table-filter-chip table-filter-chip--custom${showError ? ' table-filter-chip--invalid' : ''}`}
            title={`This filter cannot be edited here (${parsed.reason}).`}
          >
            <span className="table-filter-chip-text">Custom filter</span>
          </span>
        ) : (
          conditions.map((condition, index) => {
            const label = conditionLabel(condition, columns);
            const invalid = errorTarget === index;
            return (
              <span
                key={condition.id}
                className={`table-filter-chip${invalid ? ' table-filter-chip--invalid' : ''}${
                  edit?.index === index ? ' table-filter-chip--editing' : ''
                }`}
              >
                <button
                  type="button"
                  className="table-filter-chip-edit"
                  aria-label={`Edit filter: ${label}${invalid ? ' (invalid)' : ''}`}
                  aria-describedby={invalid ? errorId : undefined}
                  aria-expanded={edit?.index === index}
                  title={label}
                  onClick={(event) =>
                    setEdit({ index, draft: { ...condition }, returnFocus: event.currentTarget })
                  }
                >
                  <span className="table-filter-chip-text">{label}</span>
                </button>
                <button
                  type="button"
                  className="table-filter-chip-remove"
                  aria-label={`Remove filter: ${label}`}
                  onClick={() => remove(index)}
                >
                  <X size={14} aria-hidden="true" />
                </button>
              </span>
            );
          })
        )}

        {canAdd && (
          <button
            ref={addButtonRef}
            type="button"
            className="table-filter-add"
            aria-expanded={edit?.index === null}
            onClick={(event) =>
              setEdit({ index: null, draft: newCondition(columns), returnFocus: event.currentTarget })
            }
            disabled={columns.length === 0}
          >
            <Plus size={14} aria-hidden="true" />
            Add filter
          </button>
        )}

        {hasFilter && (
          <button type="button" className="table-filter-clear" onClick={clearAll}>
            {parsed.kind === 'conditions' ? 'Clear all' : 'Clear'}
          </button>
        )}
      </div>

      {showError && error && (
        <div id={errorId} className="table-filter-error" role="alert">
          This filter cannot be applied: {error.message}
        </div>
      )}

      {edit && (
        <FilterEditor
          key={edit.draft.id}
          columns={columns}
          initial={edit.draft}
          isNew={edit.index === null}
          onApply={apply}
          onCancel={close}
        />
      )}
    </div>
  );
}

// --- Editor dialog ----------------------------------------------------------

interface FilterEditorProps {
  columns: TableColumn[];
  initial: FilterCondition;
  isNew: boolean;
  onApply: (condition: FilterCondition) => void;
  onCancel: () => void;
}

function FilterEditor({ columns, initial, isNew, onApply, onCancel }: FilterEditorProps) {
  const [draft, setDraft] = useState<FilterCondition>(initial);
  const [touched, setTouched] = useState(!isNew);
  // Raw text of a one-per-line list input, kept so typing is not rewritten.
  const [linesText, setLinesText] = useState(() =>
    Array.isArray(initial.value) ? initial.value.map(String).join('\n') : '',
  );
  const dialogRef = useRef<HTMLDivElement>(null);
  const firstFieldRef = useRef<HTMLSelectElement>(null);
  const uid = useId();

  useEffect(() => {
    firstFieldRef.current?.focus();
  }, []);

  // A click outside closes.
  useEffect(() => {
    const onDown = (event: MouseEvent) => {
      if (dialogRef.current && !dialogRef.current.contains(event.target as Node)) onCancel();
    };
    document.addEventListener('mousedown', onDown);
    return () => document.removeEventListener('mousedown', onDown);
  }, [onCancel]);

  const column = findColumn(columns, draft.column);
  const ops = column ? opsForColumn(column) : [];
  const kind = column ? valueKind(column, draft.op) : 'none';
  const problem = conditionError(draft, columns);

  const update = (patch: Partial<FilterCondition>) => {
    setTouched(true);
    setDraft((prev) => ({ ...prev, ...patch }));
  };

  const changeColumn = (name: string) => {
    const next = findColumn(columns, name);
    if (!next) return;
    const op = opsForColumn(next)[0].op;
    setLinesText('');
    update({ column: next.name, op, value: emptyValue(valueKind(next, op)) });
  };

  const changeOp = (op: FilterOp) => {
    if (!column) return;
    const nextKind = valueKind(column, op);
    if (nextKind === kind) {
      update({ op });
    } else {
      setLinesText('');
      update({ op, value: emptyValue(nextKind) });
    }
  };

  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (problem === null) onApply(draft);
  };

  const onKeyDown = (event: ReactKeyboardEvent) => {
    if (event.key === 'Escape') {
      event.preventDefault();
      event.stopPropagation();
      onCancel();
    }
  };

  const options = column?.options ?? [];
  const toggle = <T,>(list: T[], item: T, on: boolean): T[] =>
    on ? (list.includes(item) ? list : [...list, item]) : list.filter((x) => x !== item);

  let valueInput: ReactNode = null;
  switch (kind) {
    case 'text':
    case 'number':
      valueInput = (
        <label className="table-filter-field">
          <span className="table-filter-field-label">Value</span>
          <input
            type="text"
            inputMode={kind === 'number' ? 'decimal' : undefined}
            className="table-filter-input"
            value={typeof draft.value === 'string' ? draft.value : ''}
            onChange={(event) => update({ value: event.target.value })}
            placeholder={kind === 'number' ? 'e.g. 12.5' : undefined}
            autoComplete="off"
          />
        </label>
      );
      break;
    case 'pair': {
      const [lo, hi] = Array.isArray(draft.value) ? (draft.value as string[]) : ['', ''];
      valueInput = (
        <div className="table-filter-pair">
          <label className="table-filter-field">
            <span className="table-filter-field-label">From</span>
            <input
              type="text"
              inputMode="decimal"
              className="table-filter-input"
              value={lo ?? ''}
              onChange={(event) => update({ value: [event.target.value, hi ?? ''] })}
              autoComplete="off"
            />
          </label>
          <label className="table-filter-field">
            <span className="table-filter-field-label">To</span>
            <input
              type="text"
              inputMode="decimal"
              className="table-filter-input"
              value={hi ?? ''}
              onChange={(event) => update({ value: [lo ?? '', event.target.value] })}
              autoComplete="off"
            />
          </label>
        </div>
      );
      break;
    }
    case 'boolean':
      valueInput = (
        <fieldset className="table-filter-fieldset">
          <legend className="table-filter-field-label">Value</legend>
          <div className="table-filter-segmented">
            {[true, false].map((flag) => (
              <label key={String(flag)} className="table-filter-segment">
                <input
                  type="radio"
                  name={`${uid}-bool`}
                  checked={draft.value === flag}
                  onChange={() => update({ value: flag })}
                />
                <span>{String(flag)}</span>
              </label>
            ))}
          </div>
        </fieldset>
      );
      break;
    case 'booleans': {
      const chosen = Array.isArray(draft.value) ? (draft.value as boolean[]) : [];
      valueInput = (
        <fieldset className="table-filter-fieldset">
          <legend className="table-filter-field-label">Values</legend>
          {[true, false].map((flag) => (
            <label key={String(flag)} className="table-filter-check">
              <input
                type="checkbox"
                checked={chosen.includes(flag)}
                onChange={(event) => update({ value: toggle(chosen, flag, event.target.checked) })}
              />
              <span>{String(flag)}</span>
            </label>
          ))}
        </fieldset>
      );
      break;
    }
    case 'option':
      valueInput = (
        <label className="table-filter-field">
          <span className="table-filter-field-label">Value</span>
          <select
            className="table-filter-select"
            value={typeof draft.value === 'string' ? draft.value : ''}
            onChange={(event) => update({ value: event.target.value })}
          >
            <option value="" disabled>
              Choose...
            </option>
            {options.map((option) => (
              <option key={option} value={option}>
                {option}
              </option>
            ))}
          </select>
        </label>
      );
      break;
    case 'options': {
      const chosen = Array.isArray(draft.value) ? (draft.value as string[]) : [];
      valueInput = (
        <fieldset className="table-filter-fieldset">
          <legend className="table-filter-field-label">Values</legend>
          <div className="table-filter-checklist">
            {options.map((option) => (
              <label key={option} className="table-filter-check">
                <input
                  type="checkbox"
                  checked={chosen.includes(option)}
                  onChange={(event) =>
                    update({ value: toggle(chosen, option, event.target.checked) as FilterValue })
                  }
                />
                <span>{option}</span>
              </label>
            ))}
          </div>
        </fieldset>
      );
      break;
    }
    case 'lines': {
      const numbers = column?.type === 'number';
      valueInput = (
        <label className="table-filter-field">
          <span className="table-filter-field-label">
            {numbers ? 'Values (one per line or comma separated)' : 'Values (one per line)'}
          </span>
          <textarea
            className="table-filter-input table-filter-textarea"
            rows={3}
            value={linesText}
            onChange={(event) => {
              setLinesText(event.target.value);
              update({ value: splitLines(event.target.value, numbers) });
            }}
          />
        </label>
      );
      break;
    }
    default:
      valueInput = null;
  }

  const title = isNew ? 'Add filter' : 'Edit filter';
  return (
    <div
      ref={dialogRef}
      className="table-filter-popover"
      role="dialog"
      aria-label={title}
      onKeyDown={onKeyDown}
    >
      <form className="table-filter-form" onSubmit={submit} noValidate>
        <label className="table-filter-field">
          <span className="table-filter-field-label">Column</span>
          <select
            ref={firstFieldRef}
            className="table-filter-select"
            value={column?.name ?? ''}
            onChange={(event) => changeColumn(event.target.value)}
          >
            {columns.map((col) => (
              <option key={col.key} value={col.name}>
                {col.name}
              </option>
            ))}
          </select>
        </label>
        <label className="table-filter-field">
          <span className="table-filter-field-label">Condition</span>
          <select
            className="table-filter-select"
            value={draft.op}
            onChange={(event) => changeOp(event.target.value as FilterOp)}
          >
            {ops.map((choice) => (
              <option key={choice.op} value={choice.op}>
                {choice.label}
              </option>
            ))}
          </select>
        </label>
        {valueInput}
        <div className="table-filter-hint" aria-live="polite">
          {touched && problem ? problem : ''}
        </div>
        <div className="table-filter-actions">
          <button type="button" className="table-filter-cancel" onClick={onCancel}>
            Cancel
          </button>
          <button type="submit" className="table-filter-apply" disabled={problem !== null}>
            Apply
          </button>
        </div>
      </form>
    </div>
  );
}
