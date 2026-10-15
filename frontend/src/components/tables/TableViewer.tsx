/**
 * Quest Tables viewer (/tables/<id>): one table's page -- its header (title
 * menu, chips), the rows grid with server-side sort and paging, and a
 * footer naming the last writer, update time, row count and schema version.
 *
 * Data: useTable (the detail, kept current by `table_changed`) and
 * useTableRows (one grid page). Loading / error / not-found states follow
 * DocViewer.
 *
 * The URL is the single source of truth for the filter and the sort: the
 * route's `filter` / `sort` params (utils/tableFilter searchParamToFilter /
 * parseSortParam, the first `[{column, direction}]` key). The filter bar
 * (TableFilterBar) and the grid's sort headers report changes, and the viewer
 * navigates to the new `?filter=` / `?sort=` with `replace: true`, keeping
 * any other params (tableViewerSearch). A filter the bar cannot show as chips
 * is still sent as-is (it is valid DSL); the bar shows "Custom filter". A
 * sort or filter naming a column the loaded schema no longer has (dropped or
 * renamed) is not sent (a 400 unknown_column); the bar says so for a filter.
 *
 * Viewer state (`TableViewerState`, kept per table: the inner component is
 * keyed by the id) is only `offset`, the grid page start: local, not in the
 * URL (a link opens on the first page). It goes back to 0 whenever the sort
 * or the filter param changes, and steps back to the last page when the
 * total shrinks below it (clampOffset).
 *
 * Rows re-query on every accepted `table_changed` (`rowsRefreshKey`): an
 * event without `updated_at` (a delete or a share-roster change; useTable
 * re-fetches the detail) does not, an event whose `schema_version` differs from the loaded schema waits for the
 * detail re-fetch instead (old column names would 400 and the rows would be
 * keyed by columns the loaded schema lacks), and a schema_version change of
 * the loaded detail always re-queries. A rows page whose schema_version
 * is newer than the detail's asks useTable for a re-fetch (an older page is
 * a rows response that lags, not a stale detail).
 *
 * Plug-in points for later phases: the header's `onOpenColumns` / `onOpenHistory` / `onOpenShare`
 * (B5 / B6), left unset so the menu shows them as disabled slots.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Link, useLocation, useNavigate } from 'react-router-dom';
import { filterErrorPath } from '../../api/tablesApi';
import type { FilterNode, SortKey } from '../../api/types';
import { useTable } from '../../hooks/useTable';
import { useTableRows } from '../../hooks/useTableRows';
import {
  filterErrorTarget,
  filterToSearchParam,
  findColumn,
  missingFilterColumns,
  searchParamToFilter,
  searchParamToSort,
  sortToSearchParam,
} from '../../utils/tableFilter';
import { tableViewerSearch, tablesListPath } from '../../utils/tablesRoute';
import { TableFilterBar } from './TableFilterBar';
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

/** The viewer's own state (filter and sort come from the route). */
export interface TableViewerState {
  offset: number;
}

/**
 * The first key of a `sort` route param (`[{"column": "<name>", "direction":
 * "asc" | "desc"}]`, a bare object is accepted too), or null when it is
 * absent or does not parse as one.
 */
export function parseSortParam(raw: string | null): SortKey | null {
  return searchParamToSort(raw)?.[0] ?? null;
}

/** The `filter` route param as a filter node (any JSON object), else null. */
export const parseFilterParam: (raw: string | null) => FilterNode | null = searchParamToFilter;

export { filterColumnNames } from '../../utils/tableFilter';

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

  const navigate = useNavigate();
  const location = useLocation();

  const [view, setView] = useState<ViewState>(() => ({ offset: 0, sortParam, filterParam }));
  // Filter or sort moved (the bar, a header click, a link, back / forward):
  // back to the first page, re-synced during render.
  if (view.sortParam !== sortParam || view.filterParam !== filterParam) {
    setView({ offset: 0, sortParam, filterParam });
  }

  const schema = table?.schema;
  const parsedFilter = useMemo(() => searchParamToFilter(filterParam), [filterParam]);
  const parsedSort = useMemo(() => parseSortParam(sortParam), [sortParam]);
  const effectiveFilter =
    parsedFilter && schema && missingFilterColumns(parsedFilter, schema).length > 0 ? null : parsedFilter;
  const effectiveSort =
    parsedSort && schema && parsedSort.column !== ROW_ID_COLUMN && !findColumn(schema, parsedSort.column)
      ? null
      : parsedSort;
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
    const change = lastChangeRef.current;
    // Deleted / share roster changed: useTable re-fetches the detail.
    if (change && change.updated_at === null) return;
    const eventVersion = change?.schema_version ?? null;
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

  // A page built on a newer schema version: the detail is behind.
  const mismatchRef = useRef<number | null>(null);
  useEffect(() => {
    if (!page || schemaVersion === null) return;
    if (page.schema_version > schemaVersion && mismatchRef.current !== page.schema_version) {
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

  // Filter / sort changes go to the URL (replace: no history entry per click).
  const navigateParams = useCallback(
    (next: { filter: string | null; sort: string | null }) => {
      navigate(
        { pathname: location.pathname, search: tableViewerSearch(location.search, next) },
        { replace: true },
      );
    },
    [navigate, location.pathname, location.search],
  );
  const setSort = useCallback(
    (next: SortKey | null) => {
      navigateParams({ filter: filterParam, sort: sortToSearchParam(next ? [next] : null) });
    },
    [navigateParams, filterParam],
  );
  const setFilter = useCallback(
    (next: FilterNode | null) => {
      navigateParams({ filter: filterToSearchParam(next), sort: sortParam });
    },
    [navigateParams, sortParam],
  );
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
  // A refusal of the filter shows on the bar; anything else (sort, paging,
  // the network) shows here.
  const errorPath = rowsError ? filterErrorPath(rowsError) : null;
  const filterError =
    rowsError && errorPath !== null && filterErrorTarget(errorPath, 0) !== null
      ? { path: errorPath, message: rowsError.message }
      : null;

  return (
    <div className="table-viewer">
      <TableHeader
        key={table.id}
        table={table}
        onDetailApplied={applyDetail}
        onRowApplied={applyRow}
        onStale={() => void refresh()}
      />

      <TableFilterBar
        columns={table.schema}
        filter={parsedFilter}
        invalidParam={filterParam !== null && parsedFilter === null}
        onChange={setFilter}
        error={filterError}
      />

      {rowsError && !filterError && (
        <div className="table-viewer-rows-error" role="alert">
          <span>Failed to load rows: {rowsError.message}</span>
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
