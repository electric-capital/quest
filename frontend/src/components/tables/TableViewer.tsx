/**
 * Quest Tables viewer (/tables/<id>): one table's page -- its header (title
 * menu, chips), the rows grid with server-side sort and paging, and a
 * footer naming the last writer, update time, row count and schema version.
 *
 * Data: useTable (the detail, kept current by `table_changed`) and
 * useTableRows (one grid page). Loading / error / not-found states follow
 * DocViewer.
 *
 * Viewer state (`TableViewerState`, kept per table: the inner component is
 * keyed by the id):
 * - `sort`: one SortKey by column NAME, or null. Initialized from the `sort`
 *   route param when it parses as `[{column, direction}]` (parseSortParam)
 *   and re-read whenever that param changes.
 * - `offset`: the grid page start; back to 0 whenever the sort or the filter
 *   changes, and stepped back to the last page when the total shrinks below
 *   it (clampOffset).
 * The filter is the route's `filter` param, parsed as JSON (parseFilterParam;
 * the filter bar of phase B4 owns building / validating it and the URL
 * sync). A sort or filter naming a column the loaded schema no longer has
 * (dropped or renamed) is ignored rather than sent (a 400 unknown_column).
 *
 * Rows re-query on every accepted `table_changed` (`rowsRefreshKey`): an
 * event whose `schema_version` differs from the loaded schema waits for the
 * detail re-fetch instead (old column names would 400 and the rows would be
 * keyed by columns the loaded schema lacks), and a schema_version change of
 * the loaded detail always re-queries. A rows page whose schema_version
 * differs from the detail's asks useTable for a re-fetch.
 *
 * Plug-in points for later phases: the filter bar slot below the header
 * (B4), and the header's `onOpenColumns` / `onOpenHistory` / `onOpenShare`
 * (B5 / B6), left unset so the menu shows them as disabled slots.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { filterErrorPath } from '../../api/tablesApi';
import type { FilterNode, SortKey } from '../../api/types';
import { useTable } from '../../hooks/useTable';
import { useTableRows } from '../../hooks/useTableRows';
import { tablesListPath } from '../../utils/tablesRoute';
import { ROW_ID_COLUMN, TABLE_GRID_PAGE_SIZE, TableGrid, clampOffset } from './TableGrid';
import { TableFooter } from './TableFooter';
import { TableHeader } from './TableHeader';
import '../docs/DocViewer.css';
import './TableViewer.css';

export interface TableViewerProps {
  tableId: string;
  /** Raw `filter` route param (filter DSL JSON), or null. */
  filter: string | null;
  /** Raw `sort` route param (`[{column, direction}]` JSON), or null. */
  sort: string | null;
}

/** The viewer's own state (the filter comes from the route). */
export interface TableViewerState {
  sort: SortKey | null;
  offset: number;
}

/**
 * The first key of a `sort` route param (`[{"column": "<name>", "direction":
 * "asc" | "desc"}]`, a bare object is accepted too), or null when it is
 * absent or does not parse as one.
 */
export function parseSortParam(raw: string | null): SortKey | null {
  if (!raw) return null;
  let value: unknown;
  try {
    value = JSON.parse(raw);
  } catch {
    return null;
  }
  const first = Array.isArray(value) ? value[0] : value;
  if (!first || typeof first !== 'object') return null;
  const { column, direction } = first as { column?: unknown; direction?: unknown };
  if (typeof column !== 'string' || !column) return null;
  if (direction !== 'asc' && direction !== 'desc') return null;
  return { column, direction };
}

/** The `filter` route param as a filter node (any JSON object), else null. */
export function parseFilterParam(raw: string | null): FilterNode | null {
  if (!raw) return null;
  try {
    const value: unknown = JSON.parse(raw);
    return value && typeof value === 'object' && !Array.isArray(value) ? (value as FilterNode) : null;
  } catch {
    return null;
  }
}

/** Every column name a filter's leaves reference. */
export function filterColumnNames(node: FilterNode): string[] {
  const out: string[] = [];
  const walk = (n: unknown) => {
    if (!n || typeof n !== 'object') return;
    const rec = n as Record<string, unknown>;
    if (Array.isArray(rec.and)) rec.and.forEach(walk);
    else if (Array.isArray(rec.or)) rec.or.forEach(walk);
    else if ('not' in rec) walk(rec.not);
    else if (typeof rec.column === 'string') out.push(rec.column);
  };
  walk(node);
  return out;
}

export function TableViewer({ tableId, filter, sort }: TableViewerProps) {
  return <TableViewerBody key={tableId} tableId={tableId} filterParam={filter} sortParam={sort} />;
}

interface ViewState extends TableViewerState {
  // The route params the state was last synced with.
  sortParam: string | null;
  filterParam: string | null;
}

function TableViewerBody({
  tableId,
  filterParam,
  sortParam,
}: {
  tableId: string;
  filterParam: string | null;
  sortParam: string | null;
}) {
  const { table, loading, error, notFound, refresh, changeSeq, lastChange, applyDetail, applyRow } =
    useTable(tableId);

  const [view, setView] = useState<ViewState>(() => ({
    sort: parseSortParam(sortParam),
    offset: 0,
    sortParam,
    filterParam,
  }));
  // Route params moved (a link, back / forward): re-sync during render.
  if (view.sortParam !== sortParam || view.filterParam !== filterParam) {
    setView({
      sort: view.sortParam !== sortParam ? parseSortParam(sortParam) : view.sort,
      offset: 0,
      sortParam,
      filterParam,
    });
  }

  const schema = table?.schema;
  const columnNames = useMemo(
    () => (schema ? new Set(schema.map((column) => column.name)) : null),
    [schema],
  );
  const parsedFilter = useMemo(() => parseFilterParam(filterParam), [filterParam]);
  const effectiveFilter =
    parsedFilter && columnNames && filterColumnNames(parsedFilter).some((name) => !columnNames.has(name))
      ? null
      : parsedFilter;
  const effectiveSort =
    view.sort && columnNames && view.sort.column !== ROW_ID_COLUMN && !columnNames.has(view.sort.column)
      ? null
      : view.sort;
  const sortKeys = useMemo(() => (effectiveSort ? [effectiveSort] : null), [effectiveSort]);

  // Rows refresh counter (see the module docstring).
  const [rowsRefreshKey, setRowsRefreshKey] = useState(0);
  const schemaVersion = table?.schema_version ?? null;
  const schemaVersionRef = useRef(schemaVersion);
  schemaVersionRef.current = schemaVersion;
  const lastChangeRef = useRef(lastChange);
  lastChangeRef.current = lastChange;
  useEffect(() => {
    if (changeSeq === 0) return;
    const eventVersion = lastChangeRef.current?.schema_version ?? null;
    if (eventVersion === null || eventVersion === schemaVersionRef.current) {
      setRowsRefreshKey((key) => key + 1);
    }
  }, [changeSeq]);
  const prevSchemaVersionRef = useRef(schemaVersion);
  useEffect(() => {
    const prev = prevSchemaVersionRef.current;
    prevSchemaVersionRef.current = schemaVersion;
    if (prev !== null && schemaVersion !== null && prev !== schemaVersion) {
      setRowsRefreshKey((key) => key + 1);
    }
  }, [schemaVersion]);

  const rows = useTableRows(tableId, {
    filter: effectiveFilter,
    sort: sortKeys,
    offset: view.offset,
    limit: TABLE_GRID_PAGE_SIZE,
    refreshKey: rowsRefreshKey,
    // Columns are named against the loaded schema: wait for it.
    enabled: table !== null,
  });
  const page = rows.page;

  // A page built on another schema version: the detail is behind.
  const mismatchRef = useRef<number | null>(null);
  useEffect(() => {
    if (!page || schemaVersion === null) return;
    if (page.schema_version !== schemaVersion && mismatchRef.current !== page.schema_version) {
      mismatchRef.current = page.schema_version;
      void refresh();
    }
  }, [page, schemaVersion, refresh]);

  // The total shrank below the offset (rows deleted on the last page).
  useEffect(() => {
    if (!page || page.offset !== view.offset) return;
    const clamped = clampOffset(view.offset, TABLE_GRID_PAGE_SIZE, page.total);
    if (clamped !== view.offset) setView((prev) => ({ ...prev, offset: clamped }));
  }, [page, view.offset]);

  const setSort = useCallback((next: SortKey | null) => {
    setView((prev) => ({ ...prev, sort: next, offset: 0 }));
  }, []);
  const setOffset = useCallback((next: number) => {
    setView((prev) => ({ ...prev, offset: Math.max(0, next) }));
  }, []);

  const [retrying, setRetrying] = useState(false);
  const retry = useCallback(async () => {
    setRetrying(true);
    try {
      await refresh();
    } finally {
      setRetrying(false);
    }
  }, [refresh]);

  if (loading) {
    return <div className="doc-viewer-state">Loading...</div>;
  }

  if (notFound) {
    return (
      <div className="doc-viewer-state">
        <p className="doc-viewer-state-text">This table no longer exists or is no longer shared with you.</p>
        <Link to={tablesListPath()} className="doc-viewer-state-link">
          All tables
        </Link>
      </div>
    );
  }

  if (!table) {
    return (
      <div className="doc-viewer-state">
        <p className="doc-viewer-state-text">{error?.message || 'Failed to load the table.'}</p>
        <button
          type="button"
          className="doc-viewer-retry"
          onClick={() => void retry()}
          disabled={retrying}
        >
          {retrying ? 'Retrying...' : 'Retry'}
        </button>
      </div>
    );
  }

  const rowsError = rows.error;
  const isFilterError = rowsError !== null && filterErrorPath(rowsError) !== null;

  return (
    <div className="table-viewer">
      <TableHeader
        key={table.id}
        table={table}
        onDetailApplied={applyDetail}
        onRowApplied={applyRow}
        onStale={() => void refresh()}
      />

      {/* Filter bar slot (phase B4): it builds the filter DSL, syncs it to
          the route's `filter` param and reads `rows.error` with
          filterErrorPath to point at the offending condition. */}

      {rowsError && (
        <div className="table-viewer-rows-error" role="alert">
          <span>
            {isFilterError ? 'This filter cannot be applied: ' : 'Failed to load rows: '}
            {rowsError.message}
          </span>
          <button
            type="button"
            className="table-viewer-rows-retry"
            onClick={() => void rows.refresh()}
            disabled={rows.loading}
          >
            Retry
          </button>
        </div>
      )}

      <TableGrid
        columns={table.schema}
        page={page}
        loading={rows.loading}
        sort={effectiveSort}
        onSortChange={setSort}
        offset={view.offset}
        limit={TABLE_GRID_PAGE_SIZE}
        onOffsetChange={setOffset}
        filterActive={effectiveFilter !== null}
      />

      <TableFooter table={table} matchingCount={effectiveFilter && page ? page.total : null} />
    </div>
  );
}
