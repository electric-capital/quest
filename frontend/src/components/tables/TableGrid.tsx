/**
 * TableGrid -- the read-only rows grid of the table viewer (/tables/<id>).
 *
 * A plain <table> inside its own scroll container (no grid dependency): the
 * header row is sticky, and so is the first column, the row id (`_id`, the
 * muted "#" label). Wide tables scroll sideways inside the container, never
 * the page. Cells render per column type through utils/tableCells.
 *
 * Header cells are buttons carrying `aria-sort` on their <th>; a click
 * cycles that column none -> asc -> desc -> none (`nextSort`, one key at a
 * time, by column NAME as the rows API wants; `_id` sorts too). The viewer
 * owns the sort and the offset and re-queries the server -- the grid only
 * shows one page (`page`, 200 rows by default) and reports intents.
 *
 * Under the table, the pager: "Rows 1-200 of 350" with Prev / Next disabled
 * at the boundaries (`pageRange`). `clampOffset` is the step back the viewer
 * takes when the total shrank below its offset (rows deleted while on the
 * last page). While a refetch is in flight the previous page stays on screen,
 * dimmed (`loading`).
 *
 * Empty result: with a filter, "No rows match this filter." and a Clear
 * filter button (`onClearFilter`); without one, "No rows yet." plus, when
 * the viewer's conversations can write rows (`suggestAskQuest`), a pointer
 * to asking Quest in a chat -- rows are written by models (approval cards
 * included); the page has no row editing.
 */

import { ArrowDown, ArrowUp, ArrowUpDown } from 'lucide-react';
import type { SortKey, TableColumn, TableRowsPage } from '../../api/types';
import { TableCell } from '../../utils/tableCells';
import './TableGrid.css';

/** Rows per grid page (the server cap, TABLE_GRID_MAX_LIMIT). */
export const TABLE_GRID_PAGE_SIZE = 200;

/** The row-id pseudo column: sortable by this name, shown as "#". */
export const ROW_ID_COLUMN = '_id';

/**
 * The sort after a header click on `columnName`: another column (or none)
 * -> ascending on it; ascending -> descending; descending -> no sort.
 */
export function nextSort(current: SortKey | null, columnName: string): SortKey | null {
  if (!current || current.column !== columnName) return { column: columnName, direction: 'asc' };
  if (current.direction === 'asc') return { column: columnName, direction: 'desc' };
  return null;
}

export interface PageRange {
  /** 1-based first row shown (0 when there are none). */
  from: number;
  /** 1-based last row shown (0 when there are none). */
  to: number;
  total: number;
  hasPrev: boolean;
  hasNext: boolean;
  prevOffset: number;
  nextOffset: number;
}

/** Pager math for one page of `limit` rows starting at `offset` out of `total`. */
export function pageRange(offset: number, limit: number, total: number): PageRange {
  const safeLimit = Math.max(1, limit);
  const start = Math.max(0, offset);
  const to = Math.min(start + safeLimit, total);
  const from = total > 0 && start < total ? start + 1 : 0;
  return {
    from,
    to: from === 0 ? 0 : to,
    total,
    hasPrev: start > 0,
    hasNext: start + safeLimit < total,
    prevOffset: Math.max(0, start - safeLimit),
    nextOffset: start + safeLimit,
  };
}

/**
 * The offset to show for `total` rows: unchanged while it is inside the
 * rows, else the start of the last page (0 for an empty result). Offsets
 * snap to page starts.
 */
export function clampOffset(offset: number, limit: number, total: number): number {
  const safeLimit = Math.max(1, limit);
  if (total <= 0) return 0;
  const lastStart = Math.floor((total - 1) / safeLimit) * safeLimit;
  const pageStart = Math.floor(Math.max(0, offset) / safeLimit) * safeLimit;
  return Math.min(pageStart, lastStart);
}

function ariaSort(sort: SortKey | null, columnName: string): 'ascending' | 'descending' | 'none' {
  if (!sort || sort.column !== columnName) return 'none';
  return sort.direction === 'asc' ? 'ascending' : 'descending';
}

function SortIcon({ sort, columnName }: { sort: SortKey | null; columnName: string }) {
  const state = ariaSort(sort, columnName);
  if (state === 'ascending') return <ArrowUp size={12} aria-hidden="true" className="table-grid-sort-icon is-active" />;
  if (state === 'descending') return <ArrowDown size={12} aria-hidden="true" className="table-grid-sort-icon is-active" />;
  return <ArrowUpDown size={12} aria-hidden="true" className="table-grid-sort-icon" />;
}

export interface TableGridProps {
  /** The table's schema, in display order. */
  columns: TableColumn[];
  /** The page to show; null before the first one lands. */
  page: TableRowsPage | null;
  /** True while a (re)fetch is in flight: the shown page dims. */
  loading: boolean;
  sort: SortKey | null;
  onSortChange: (sort: SortKey | null) => void;
  /** Offset of the requested page (the pager shows it). */
  offset: number;
  limit?: number;
  onOffsetChange: (offset: number) => void;
  /** A filter is applied: an empty result says "no match", not "no rows". */
  filterActive?: boolean;
  /** Clear the filter (the empty result's Clear filter button). */
  onClearFilter?: () => void;
  /** An empty table suggests asking Quest for rows (false: rows are denied to this viewer). */
  suggestAskQuest?: boolean;
}

export const NO_ROWS_TEXT = 'No rows yet.';
export const ASK_FOR_ROWS_TEXT = 'Ask Quest to add rows in a chat.';
export const NO_MATCH_TEXT = 'No rows match this filter.';

export function TableGrid({
  columns,
  page,
  loading,
  sort,
  onSortChange,
  offset,
  limit = TABLE_GRID_PAGE_SIZE,
  onOffsetChange,
  filterActive = false,
  onClearFilter,
  suggestAskQuest = true,
}: TableGridProps) {
  if (!page) {
    return <div className="table-grid-state">{loading ? 'Loading rows...' : ''}</div>;
  }
  const range = pageRange(offset, limit, page.total);

  const header = (reactKey: string, name: string, label: string, extraClass = '', buttonLabel?: string) => {
    const direction = ariaSort(sort, name);
    return (
    <th
      key={reactKey}
      scope="col"
      // ARIA: aria-sort belongs on the sorted column only.
      aria-sort={direction === 'none' ? undefined : direction}
      className={`table-grid-th${extraClass}`}
    >
      <button
        type="button"
        className="table-grid-sort-button"
        onClick={() => onSortChange(nextSort(sort, name))}
        title={`Sort by ${buttonLabel ?? label}`}
        aria-label={buttonLabel ? `Sort by ${buttonLabel}` : undefined}
      >
        <span className="table-grid-th-label">{label}</span>
        <SortIcon sort={sort} columnName={name} />
      </button>
    </th>
    );
  };

  return (
    <div className="table-grid">
      <div
        className={`table-grid-scroll${loading ? ' is-refetching' : ''}`}
        aria-busy={loading}
      >
        <table className="table-grid-table">
          <thead>
            <tr>
              {header(ROW_ID_COLUMN, ROW_ID_COLUMN, '#', ' table-grid-th--id', 'row number')}
              {columns.map((column) =>
                header(column.key, column.name, column.name, column.type === 'number' ? ' table-grid-th--number' : ''),
              )}
            </tr>
          </thead>
          <tbody>
            {page.rows.map((row) => (
              <tr key={row._id}>
                <th scope="row" className="table-grid-row-id">
                  {row._id}
                </th>
                {columns.map((column) => (
                  <TableCell key={column.key} column={column} value={row[column.key]} />
                ))}
              </tr>
            ))}
          </tbody>
        </table>
        {page.total === 0 && (
          <div className="table-grid-empty" role="status">
            {filterActive ? (
              <>
                <p className="table-grid-empty-title">{NO_MATCH_TEXT}</p>
                {onClearFilter && (
                  <button type="button" className="table-grid-empty-action" onClick={onClearFilter}>
                    Clear filter
                  </button>
                )}
              </>
            ) : (
              <>
                <p className="table-grid-empty-title">{NO_ROWS_TEXT}</p>
                {suggestAskQuest && <p className="table-grid-empty-hint">{ASK_FOR_ROWS_TEXT}</p>}
              </>
            )}
          </div>
        )}
      </div>
      {page.total > 0 && (
        <nav className="table-grid-pager" aria-label="Pages">
          <span className="table-grid-pager-info">
            {range.from > 0 ? `Rows ${range.from}-${range.to} of ${range.total}` : `${range.total} rows`}
          </span>
          <span className="table-grid-pager-buttons">
            <button
              type="button"
              className="table-grid-pager-button"
              onClick={() => onOffsetChange(range.prevOffset)}
              disabled={!range.hasPrev}
            >
              Prev
            </button>
            <button
              type="button"
              className="table-grid-pager-button"
              onClick={() => onOffsetChange(range.nextOffset)}
              disabled={!range.hasNext}
            >
              Next
            </button>
          </span>
        </nav>
      )}
    </div>
  );
}
