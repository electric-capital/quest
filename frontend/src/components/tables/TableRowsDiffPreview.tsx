/**
 * A before / after row grid over SpreadsheetDiffPreview (variant "rows").
 * Shared by the write_table_rows approval card (preview field type
 * `table_rows_diff`, payload `field.rows_diff`) and the History detail of a
 * rows change (which maps its key-keyed row images onto TableRowsDiff,
 * values keyed by column name, before rendering). `rowsDiffToGrid()` is the
 * whole mapping:
 *
 * - Column labels are the column names (`diff.columns`, schema order); row
 *   labels the row `_id`, "new" for a row an insert would add (null `_id`),
 *   each prefixed with the grid's status marker ("+ new", "− 3", "~ 7")
 *   and the status word for screen readers.
 * - added rows show their `after` values on a green row; deleted rows their
 *   `before` values, struck through on a red row; changed rows show the
 *   cells named in `changed_columns` (or whose text differs) on amber with
 *   the old value (red, struck) above the new one, the other cells plain.
 * - Values are text (`tableValueText()`): booleans true / false, a
 *   multi-enum list joined with ", ", cut at TABLE_CELL_MAX_CHARS
 *   (`previewText()`, in the cell and its `title`) after the change test.
 *   A null, an empty multi-enum list or a missing column is the grid's
 *   muted NULL marker, distinct from an empty string.
 * - The legend names only the row kinds present. "Showing N of TOTAL rows"
 *   under the grid when `truncated`; "No rows" for an empty diff.
 */
import type { SpreadsheetDiffGrid, TableRowsDiff } from '../../api/types';
import {
  SpreadsheetDiffPreview,
  type SpreadsheetDiffNullCells,
  type SpreadsheetDiffRowStatus,
} from '../SpreadsheetDiffPreview';
import { tableValueText } from '../../utils/tableCells';
import { previewText } from './TableColumnsPreview';
import './TableRowsDiffPreview.css';

/** Row label of a row an insert would add (its `_id` is still null). */
export const NEW_ROW_LABEL = 'new';

/** Everything SpreadsheetDiffPreview needs to draw a TableRowsDiff. */
export interface TableRowsDiffGrid {
  grid: SpreadsheetDiffGrid;
  colLabels: string[];
  rowLabels: string[];
  rowStatus: SpreadsheetDiffRowStatus[];
  nullCells: Required<SpreadsheetDiffNullCells>;
}

/**
 * Map a rows diff onto the spreadsheet grid: the whole grid is the target
 * range (no context cells); `current` holds each row's before values
 * (after for an added row), `new` its after values (before for a deleted
 * row), `changed` only the changed cells of changed rows (compared on the
 * full text; the grid strings are then cut by `previewText()`).
 */
export function rowsDiffToGrid(diff: TableRowsDiff): TableRowsDiffGrid {
  const columns = diff.columns ?? [];
  const rows = diff.rows ?? [];
  const current: string[][] = [];
  const next: string[][] = [];
  const changed: boolean[][] = [];
  const nullCurrent: boolean[][] = [];
  const nullNew: boolean[][] = [];
  for (const row of rows) {
    const before = row.status === 'added' ? row.after : row.before;
    const after = row.status === 'deleted' ? row.before : row.after;
    const beforeText = columns.map((name) => tableValueText(before?.[name]));
    const afterText = columns.map((name) => tableValueText(after?.[name]));
    const changedColumns = new Set(row.changed_columns ?? []);
    current.push(beforeText.map((t) => previewText(t ?? '')));
    next.push(afterText.map((t) => previewText(t ?? '')));
    nullCurrent.push(beforeText.map((t) => t === null));
    nullNew.push(afterText.map((t) => t === null));
    changed.push(columns.map((name, c) => (
      row.status === 'changed'
      && (changedColumns.has(name) || beforeText[c] !== afterText[c])
    )));
  }
  return {
    grid: {
      start_row: 1,
      start_col: 1,
      target_start_row: 1,
      target_start_col: 1,
      target_end_row: rows.length,
      target_end_col: columns.length,
      current,
      new: next,
      changed,
    },
    colLabels: [...columns],
    rowLabels: rows.map((row) => (row._id === null || row._id === undefined
      ? NEW_ROW_LABEL : String(row._id))),
    rowStatus: rows.map((row) => row.status),
    nullCells: { current: nullCurrent, new: nullNew },
  };
}

/** The note under a truncated grid, or null when every row is shown. */
export function rowsDiffTruncationNote(diff: TableRowsDiff): string | null {
  if (!diff.truncated) return null;
  return `Showing ${diff.rows.length} of ${diff.total} rows`;
}

export interface TableRowsDiffPreviewProps {
  diff: TableRowsDiff;
}

export function TableRowsDiffPreview({ diff }: TableRowsDiffPreviewProps) {
  if ((diff.rows ?? []).length === 0 || (diff.columns ?? []).length === 0) {
    return <div className="table-rows-diff"><span className="table-rows-diff-note">No rows</span></div>;
  }
  const mapped = rowsDiffToGrid(diff);
  const note = rowsDiffTruncationNote(diff);
  return (
    <div className="table-rows-diff">
      <SpreadsheetDiffPreview
        grid={mapped.grid}
        colLabels={mapped.colLabels}
        rowLabels={mapped.rowLabels}
        rowStatus={mapped.rowStatus}
        nullCells={mapped.nullCells}
        variant="rows"
      />
      {note && <div className="table-rows-diff-note">{note}</div>}
    </div>
  );
}
