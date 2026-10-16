import type { ReactNode } from 'react';
import type { SpreadsheetDiffGrid } from '../api/types';
import './SpreadsheetDiffPreview.css';

/** Row status of the `rows` variant: the row's class and colour. */
export type SpreadsheetDiffRowStatus = 'added' | 'deleted' | 'changed';

/**
 * Visible marker before a row label when the row has a status (the status
 * word itself follows as visually hidden text): "+ 7", "− 3", "~ 9".
 */
export const SPREADSHEET_DIFF_ROW_MARKS: Record<SpreadsheetDiffRowStatus, string> = {
  added: '+',
  deleted: '−',
  changed: '~',
};

// Legend order of the row kinds in the rows variant.
const ROW_KINDS: SpreadsheetDiffRowStatus[] = ['added', 'deleted', 'changed'];

/** Visually hidden label of the rows variant's corner header cell. */
export const ROWS_CORNER_LABEL = 'Row';

/**
 * Cells that hold no value (null), as opposed to an empty string. Same shape
 * as `grid.current` / `grid.new`; a missing entry means "not null".
 */
export interface SpreadsheetDiffNullCells {
  current?: boolean[][];
  new?: boolean[][];
}

export interface SpreadsheetDiffPreviewProps {
  grid: SpreadsheetDiffGrid;
  // Header text per grid column (0-based within the grid), in place of the
  // A1 letters. A missing entry falls back to the letter.
  colLabels?: string[];
  // Header text per grid row (0-based within the grid), in place of the
  // row number. A missing entry falls back to the number.
  rowLabels?: string[];
  // Per grid row: adds `sheet-diff-row-<status>` to the <tr> (coloured in
  // the rows variant) and a status marker to the row label.
  rowStatus?: (SpreadsheetDiffRowStatus | null)[];
  // Cells to render as the muted NULL marker instead of their string.
  nullCells?: SpreadsheetDiffNullCells;
  // 'spreadsheet' (default): the edit_google_spreadsheet card. 'rows': a
  // table-row diff, where unchanged cells are plain, the row status colours
  // the row and the legend names the row kinds present.
  variant?: 'spreadsheet' | 'rows';
}

/** Convert a 1-based column index to its A1 letter run (1 -> "A"). */
function colLetters(index: number): string {
  let letters = '';
  let i = index;
  while (i > 0) {
    const rem = (i - 1) % 26;
    letters = String.fromCharCode(65 + rem) + letters;
    i = Math.floor((i - 1) / 26);
  }
  return letters;
}

/**
 * Renders the edit_google_spreadsheet approval card's diff table: the replaced
 * range plus up to 2 rows/cols of surrounding context (captured
 * server-side at proposal time), with spreadsheet-style row numbers and
 * column letters. Cells inside the replaced range show old -> new when
 * the value changes; context cells are muted.
 *
 * Optional props reuse the grid for other row diffs (Quest Tables'
 * TableRowsDiffPreview):
 *
 * - `colLabels` / `rowLabels` replace the letters and numbers.
 * - `rowStatus` classes each row (`sheet-diff-row-added` / `-deleted` /
 *   `-changed`, coloured from the shared `--diff-*` tokens of
 *   src/index.css; a deleted row's values are struck through) and prefixes
 *   its label with a marker (`SPREADSHEET_DIFF_ROW_MARKS`: "+ 7", "− 3",
 *   "~ 9") followed by the status word as visually hidden text, so the
 *   status does not rest on colour alone.
 * - `nullCells` renders a null as a muted "NULL" so it stays distinct from
 *   an empty string.
 * - `variant="rows"`: unchanged cells are plain (no accent tint), the
 *   headers lose the in-target tint (the whole grid is the target) and get
 *   `scope="col"` / `scope="row"`, the corner cell a visually hidden "Row"
 *   label, a changed cell stacks its old value (red, struck) over its new
 *   one (plain text) on two ellipsized lines, and the legend lists only the
 *   row kinds present (none: no legend).
 *
 * Without them the markup is exactly the spreadsheet card's (pinned by
 * SpreadsheetDiffPreview.test.tsx).
 */
export function SpreadsheetDiffPreview({
  grid,
  colLabels,
  rowLabels,
  rowStatus,
  nullCells,
  variant = 'spreadsheet',
}: SpreadsheetDiffPreviewProps) {
  const nRows = grid.current.length;
  const nCols = nRows > 0 ? Math.max(...grid.current.map((r) => r.length)) : 0;
  if (nRows === 0 || nCols === 0) return null;

  const inTarget = (row: number, col: number) =>
    row >= grid.target_start_row && row <= grid.target_end_row
    && col >= grid.target_start_col && col <= grid.target_end_col;

  const nullMarker = <span className="sheet-diff-null">NULL</span>;
  const currentIsNull = (r: number, c: number) => nullCells?.current?.[r]?.[c] ?? false;
  const newIsNull = (tr: number, tc: number) => nullCells?.new?.[tr]?.[tc] ?? false;

  const renderCell = (r: number, c: number) => {
    const row = grid.start_row + r;
    const col = grid.start_col + c;
    const currentValue = grid.current[r]?.[c] ?? '';
    if (!inTarget(row, col)) {
      return (
        <td key={c} className="sheet-diff-cell context" title={currentValue}>
          {currentIsNull(r, c) ? nullMarker : currentValue}
        </td>
      );
    }
    const tr = row - grid.target_start_row;
    const tc = col - grid.target_start_col;
    const newValue = grid.new[tr]?.[tc] ?? '';
    const changed = grid.changed[tr]?.[tc] ?? false;
    const newNull = newIsNull(tr, tc);
    if (!changed) {
      return (
        <td key={c} className="sheet-diff-cell target unchanged" title={newValue}>
          {newNull ? nullMarker : newValue}
        </td>
      );
    }
    const oldNull = currentIsNull(r, c);
    let newContent: ReactNode = newValue === '' ? '(empty)' : newValue;
    if (newNull) newContent = nullMarker;
    return (
      <td key={c} className="sheet-diff-cell target changed">
        {(currentValue !== '' || oldNull) && (
          <span className="sheet-diff-old" title={currentValue}>
            {oldNull ? nullMarker : currentValue}
          </span>
        )}
        <span className="sheet-diff-new" title={newValue}>
          {newContent}
        </span>
      </td>
    );
  };

  const rowsVariant = variant === 'rows';
  // The rows variant's whole grid is the target: no in-target header tint.
  const headerTarget = (isTarget: boolean) => (isTarget && !rowsVariant ? ' in-target' : '');
  const legendKinds = ROW_KINDS.filter((kind) => rowStatus?.includes(kind));

  return (
    <div className="sheet-diff-scroll">
      <table className={rowsVariant ? 'sheet-diff-table rows-variant' : 'sheet-diff-table'}>
        <thead>
          <tr>
            {rowsVariant ? (
              <th className="sheet-diff-corner" scope="col">
                <span className="sheet-diff-sr-only">{ROWS_CORNER_LABEL}</span>
              </th>
            ) : (
              <th className="sheet-diff-corner" />
            )}
            {Array.from({ length: nCols }, (_, c) => {
              const col = grid.start_col + c;
              const isTargetCol = col >= grid.target_start_col && col <= grid.target_end_col;
              return (
                <th
                  key={c}
                  className={`sheet-diff-col-label${headerTarget(isTargetCol)}`}
                  scope={rowsVariant ? 'col' : undefined}
                >
                  {colLabels?.[c] ?? colLetters(col)}
                </th>
              );
            })}
          </tr>
        </thead>
        <tbody>
          {Array.from({ length: nRows }, (_, r) => {
            const row = grid.start_row + r;
            const isTargetRow = row >= grid.target_start_row && row <= grid.target_end_row;
            const status = rowStatus?.[r];
            return (
              <tr key={r} className={status ? `sheet-diff-row-${status}` : undefined}>
                <th
                  className={`sheet-diff-row-label${headerTarget(isTargetRow)}`}
                  scope={rowsVariant ? 'row' : undefined}
                >
                  {status && (
                    <span className="sheet-diff-row-mark" aria-hidden="true">
                      {SPREADSHEET_DIFF_ROW_MARKS[status]}{' '}
                    </span>
                  )}
                  {rowLabels?.[r] ?? row}
                  {status && <span className="sheet-diff-sr-only"> {status}</span>}
                </th>
                {Array.from({ length: nCols }, (_, c) => renderCell(r, c))}
              </tr>
            );
          })}
        </tbody>
      </table>
      {rowsVariant ? (
        legendKinds.length > 0 && (
          <div className="sheet-diff-legend">
            {legendKinds.map((kind) => (
              <span key={kind} className="sheet-diff-legend-item">
                <span className={`sheet-diff-swatch row-${kind}`} /> {kind}
              </span>
            ))}
          </div>
        )
      ) : (
        <div className="sheet-diff-legend">
          <span className="sheet-diff-legend-item">
            <span className="sheet-diff-swatch changed" /> changing
          </span>
          <span className="sheet-diff-legend-item">
            <span className="sheet-diff-swatch unchanged" /> rewritten, same value
          </span>
          <span className="sheet-diff-legend-item">
            <span className="sheet-diff-swatch context" /> context
          </span>
        </div>
      )}
    </div>
  );
}
