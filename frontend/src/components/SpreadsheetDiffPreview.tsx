import type { SpreadsheetDiffGrid } from '../api/types';
import './SpreadsheetDiffPreview.css';

interface SpreadsheetDiffPreviewProps {
  grid: SpreadsheetDiffGrid;
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
 */
export function SpreadsheetDiffPreview({ grid }: SpreadsheetDiffPreviewProps) {
  const nRows = grid.current.length;
  const nCols = nRows > 0 ? Math.max(...grid.current.map((r) => r.length)) : 0;
  if (nRows === 0 || nCols === 0) return null;

  const inTarget = (row: number, col: number) =>
    row >= grid.target_start_row && row <= grid.target_end_row
    && col >= grid.target_start_col && col <= grid.target_end_col;

  const renderCell = (r: number, c: number) => {
    const row = grid.start_row + r;
    const col = grid.start_col + c;
    const currentValue = grid.current[r]?.[c] ?? '';
    if (!inTarget(row, col)) {
      return (
        <td key={c} className="sheet-diff-cell context" title={currentValue}>
          {currentValue}
        </td>
      );
    }
    const tr = row - grid.target_start_row;
    const tc = col - grid.target_start_col;
    const newValue = grid.new[tr]?.[tc] ?? '';
    const changed = grid.changed[tr]?.[tc] ?? false;
    if (!changed) {
      return (
        <td key={c} className="sheet-diff-cell target unchanged" title={newValue}>
          {newValue}
        </td>
      );
    }
    return (
      <td key={c} className="sheet-diff-cell target changed">
        {currentValue !== '' && (
          <span className="sheet-diff-old" title={currentValue}>{currentValue}</span>
        )}
        <span className="sheet-diff-new" title={newValue}>
          {newValue === '' ? '(empty)' : newValue}
        </span>
      </td>
    );
  };

  return (
    <div className="sheet-diff-scroll">
      <table className="sheet-diff-table">
        <thead>
          <tr>
            <th className="sheet-diff-corner" />
            {Array.from({ length: nCols }, (_, c) => {
              const col = grid.start_col + c;
              const isTargetCol = col >= grid.target_start_col && col <= grid.target_end_col;
              return (
                <th key={c} className={`sheet-diff-col-label${isTargetCol ? ' in-target' : ''}`}>
                  {colLetters(col)}
                </th>
              );
            })}
          </tr>
        </thead>
        <tbody>
          {Array.from({ length: nRows }, (_, r) => {
            const row = grid.start_row + r;
            const isTargetRow = row >= grid.target_start_row && row <= grid.target_end_row;
            return (
              <tr key={r}>
                <th className={`sheet-diff-row-label${isTargetRow ? ' in-target' : ''}`}>
                  {row}
                </th>
                {Array.from({ length: nCols }, (_, c) => renderCell(r, c))}
              </tr>
            );
          })}
        </tbody>
      </table>
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
    </div>
  );
}
