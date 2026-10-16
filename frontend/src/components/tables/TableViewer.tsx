/**
 * Quest Tables viewer (/tables/<id>): one table's page -- its header (title
 * menu, chips), the rows grid with server-side sort and paging, and a
 * footer naming the last writer, update time, row count and schema version.
 *
 * Data: useTable (the detail, kept current by `table_changed`) and
 * useTableRows (one grid page). Loading / error / not-found states follow
 * DocViewer. A navigation that carries the detail as router state (the
 * New Table dialog, `tableFromLocationState`) opens on it, with the first
 * fetch as a background refresh.
 *
 * The URL is the single source of truth for the filter and the sort: the
 * route's `filter` / `sort` params (utils/tableFilter searchParamToFilter /
 * searchParamToSort). The filter bar (TableFilterBar) and the grid's sort
 * headers report changes, and the viewer navigates to the new `?filter=` /
 * `?sort=` with `replace: true`, keeping any other params
 * (tableViewerSearch). A filter the bar cannot show as chips is still sent
 * as-is (it is valid DSL); the bar shows "Custom filter".
 *
 * Sort: a link may carry up to 3 keys (`resolveSort`); every key whose
 * column exists (or a row field: `_id`, `_created_at`, `_updated_at`) is
 * sent, in order. The grid header shows the primary key's arrow only, and
 * a header click replaces the whole sort with that one column's next state
 * (`nextSort`: single-key cycling), so secondary keys are dropped then. A
 * sort param that does not parse (bad JSON or direction, more than 3 keys,
 * a column named twice) is not sent and a note says so with Clear sort.
 *
 * Stale columns: a filter naming a column the loaded schema no longer has
 * (dropped or renamed) is not sent (a 400 unknown_column) and the bar says
 * so; a sort key naming one is left out (the other keys are still sent) and
 * a note under the bar names it, with Clear sort.
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
 * Side panels, one at a time (`panel`, a `TableSidePanel` or null; local,
 * not in the URL):
 * - 'columns' (the header's Columns item, everyone): TableSchemaEditor,
 *   read-only for non-owners, beside the grid (it narrows the grid; the
 *   body gets `table-viewer-body--panel`), a full-screen sheet on phones
 *   (TableSchemaEditor.css). It applies a saved detail through
 *   useTable.applyDetail and asks for a re-fetch on a conflict. It owns its
 *   draft and discard confirm: leaving it for another panel goes through
 *   its ref handle (`requestClose(then)`), which confirms when there are
 *   edits; its own Close and Escape inside it clear `panel` (after the
 *   confirm when there are edits).
 * - 'history' (the header's History item, which the header shows to
 *   editors only; the mount is not gated, the drawer shows its own
 *   lost-access state): TableHistory, a drawer fixed over the right edge
 *   (full screen on phones, TableHistory.css), so it takes no room in the
 *   grid layout. A restore
 *   applies `res.table` through applyDetail. A restore always bumps
 *   `schema_version` (chat/tables/revert.py), so the grid re-queries through
 *   the version change above; should the shown version already be the
 *   restored one (the table_changed re-fetch landed first, and re-queried
 *   then), the viewer bumps the rows refresh key itself, so the grid can
 *   never keep showing pre-restore rows.
 *
 * Closing a panel (its Close, Escape inside it, a discard confirm)
 * returns focus to the header's title (menu) button, which opened it --
 * unless focus already went somewhere else on the page. Switching panels
 * leaves focus to the new panel (its heading).
 *
 * Empty results: the grid says how rows get in (asking Quest, unless rows
 * are denied to this viewer) or, under a filter, offers Clear filter,
 * which navigates like the bar's.
 *
 * Plug-in point for a later phase: the header's `onOpenShare` (phase C2),
 * left unset so the menu shows Share as a disabled slot.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Link, useLocation, useNavigate } from 'react-router-dom';
import { filterErrorPath } from '../../api/tablesApi';
import type { FilterNode, RestoreTableResponse, SortKey, TableColumn } from '../../api/types';
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
import { tableFromLocationState, tableViewerSearch, tablesListPath } from '../../utils/tablesRoute';
import { TableFilterBar } from './TableFilterBar';
import { TABLE_GRID_PAGE_SIZE, TableGrid, clampOffset } from './TableGrid';
import { TableFooter } from './TableFooter';
import { TableHeader } from './TableHeader';
import { TableHistory } from './TableHistory';
import { TableSchemaEditor, type TableSchemaEditorHandle } from './TableSchemaEditor';
import '../docs/DocViewer.css';
import './TableViewer.css';

export interface TableViewerProps {
  tableId: string;
  /** Raw `filter` route param (filter DSL JSON), or null. */
  filter: string | null;
  /** Raw `sort` route param (`[{column, direction}]` JSON), or null. */
  sort: string | null;
}

/** The side panel open over / beside the grid (one at a time). */
export type TableSidePanel = 'columns' | 'history';

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

/** Row fields the server sorts by (filters.py `_ROW_FIELD_SQL`). */
const SORT_ROW_FIELDS = ['_id', '_created_at', '_updated_at'];

/** How the viewer reads a `sort` route param against the loaded schema. */
export interface ResolvedSort {
  /** The keys to send (schema spelling), in order; null for none. */
  keys: SortKey[] | null;
  /** Column names the schema lacks (left out of `keys`). */
  missing: string[];
  /** The param is present but cannot be read (not sent at all). */
  unreadable: boolean;
}

/**
 * Resolve a raw `sort` param: every parsed key whose column exists (or a
 * row field) is kept, named as the schema spells it. Two keys resolving to
 * the same column (names differing only in case) make the param unreadable,
 * as the server refuses a column sorted twice. Before the schema loads
 * (`schema` null) every key is kept.
 */
export function resolveSort(raw: string | null, schema: TableColumn[] | null): ResolvedSort {
  if (raw === null) return { keys: null, missing: [], unreadable: false };
  const parsed = searchParamToSort(raw);
  if (!parsed) return { keys: null, missing: [], unreadable: true };
  if (!schema) return { keys: parsed, missing: [], unreadable: false };
  const keys: SortKey[] = [];
  const missing: string[] = [];
  const seen = new Set<string>();
  for (const key of parsed) {
    const rowField = SORT_ROW_FIELDS.find((field) => field === key.column.toLowerCase());
    const column = rowField ? null : findColumn(schema, key.column);
    const identity = rowField ?? column?.key ?? null;
    if (identity === null) {
      if (!missing.includes(key.column)) missing.push(key.column);
      continue;
    }
    if (seen.has(identity)) return { keys: null, missing: [], unreadable: true };
    seen.add(identity);
    keys.push({ column: rowField ?? column!.name, direction: key.direction });
  }
  return { keys: keys.length > 0 ? keys : null, missing, unreadable: false };
}

/** The note under the filter bar for a sort that is not (fully) applied. */
function sortNote(sort: ResolvedSort): string {
  if (sort.unreadable) return "This link's sort could not be read.";
  return sort.missing.length === 1
    ? `This sort references a column that no longer exists (${sort.missing[0]}).`
    : `This sort references columns that no longer exist (${sort.missing.join(', ')}).`;
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
  const navigate = useNavigate();
  const location = useLocation();
  // The detail a navigation here carried (the New Table dialog's create
  // response), read once per table: the body is keyed by the id.
  const [initial] = useState(() => tableFromLocationState(location.state, tableId));
  const { table, loading, error, notFound, refresh, changeSeq, lastChange, applyDetail, applyRow } =
    useTable(tableId, { initial });

  const [view, setView] = useState<ViewState>(() => ({ offset: 0, sortParam, filterParam }));
  // Filter or sort moved (the bar, a header click, a link, back / forward):
  // back to the first page, re-synced during render.
  if (view.sortParam !== sortParam || view.filterParam !== filterParam) {
    setView({ offset: 0, sortParam, filterParam });
  }

  const schema = table?.schema;
  const parsedFilter = useMemo(() => searchParamToFilter(filterParam), [filterParam]);
  const effectiveFilter =
    parsedFilter && schema && missingFilterColumns(parsedFilter, schema).length > 0 ? null : parsedFilter;
  const resolvedSort = useMemo(() => resolveSort(sortParam, schema ?? null), [sortParam, schema]);
  const sortKeysJson = resolvedSort.keys ? JSON.stringify(resolvedSort.keys) : null;
  // Stable identity per content, so a schema object refresh does not re-query.
  const sortKeys = useMemo<SortKey[] | null>(
    () => (sortKeysJson ? (JSON.parse(sortKeysJson) as SortKey[]) : null),
    [sortKeysJson],
  );
  // The grid shows (and cycles) the primary key only.
  const primarySort = sortKeys?.[0] ?? null;

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

  const [panel, setPanel] = useState<TableSidePanel | null>(null);
  const panelRef = useRef(panel);
  panelRef.current = panel;
  const editorRef = useRef<TableSchemaEditorHandle>(null);
  const openPanel = useCallback((next: TableSidePanel) => {
    if (panelRef.current === next) return;
    // Leaving the Columns panel goes through its discard confirm.
    if (panelRef.current === 'columns' && editorRef.current) {
      editorRef.current.requestClose(() => setPanel(next));
    } else {
      setPanel(next);
    }
  }, []);
  const openColumns = useCallback(() => openPanel('columns'), [openPanel]);
  const openHistory = useCallback(() => openPanel('history'), [openPanel]);
  // Focus goes back to the menu button that opened the panel, once the
  // panel is gone (when focus fell to the page with it).
  const menuButtonRef = useRef<HTMLButtonElement>(null);
  const [panelClosed, setPanelClosed] = useState(0);
  const closePanel = useCallback(() => {
    setPanel(null);
    setPanelClosed((n) => n + 1);
  }, []);
  useEffect(() => {
    if (panelClosed === 0) return;
    const active = document.activeElement;
    if (!active || active === document.body || !active.isConnected) menuButtonRef.current?.focus();
  }, [panelClosed]);
  const handleRestored = useCallback(
    (res: RestoreTableResponse) => {
      const shownVersion = schemaVersionRef.current;
      applyDetail(res.table);
      if (res.table.schema_version === shownVersion) setRowsRefreshKey((key) => key + 1);
    },
    [applyDetail],
  );

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
        onOpenColumns={openColumns}
        onOpenHistory={openHistory}
        menuButtonRef={menuButtonRef}
      />

      <div className={`table-viewer-body${panel === 'columns' ? ' table-viewer-body--panel' : ''}`}>
        <div className="table-viewer-main">
          <TableFilterBar
            columns={table.schema}
            filter={parsedFilter}
            invalidParam={filterParam !== null && parsedFilter === null}
            onChange={setFilter}
            error={filterError}
          />

          {(resolvedSort.unreadable || resolvedSort.missing.length > 0) && (
            <div className="table-filter-row table-sort-note">
              <span className="table-filter-note">{sortNote(resolvedSort)}</span>
              <button type="button" className="table-filter-clear" onClick={() => setSort(null)}>
                Clear sort
              </button>
            </div>
          )}

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
            sort={primarySort}
            onSortChange={setSort}
            offset={view.offset}
            limit={TABLE_GRID_PAGE_SIZE}
            onOffsetChange={setOffset}
            filterActive={effectiveFilter !== null}
            onClearFilter={() => setFilter(null)}
            suggestAskQuest={table.access.rows !== 'denied'}
          />

          <TableFooter table={table} matchingCount={effectiveFilter && page ? page.total : null} />
        </div>
        {panel === 'columns' && (
          <TableSchemaEditor
            ref={editorRef}
            table={table}
            onClose={closePanel}
            onDetailApplied={applyDetail}
            onRefresh={() => void refresh()}
          />
        )}
      </div>

      {panel === 'history' && <TableHistory table={table} onClose={closePanel} onRestored={handleRestored} />}
    </div>
  );
}
