/**
 * One page of a table's rows for the viewer grid (GET /tables/{id}/rows via
 * queryTableRows), re-queried when the parameters change and when
 * `refreshKey` moves (the viewer passes a counter it bumps on accepted
 * `table_changed` events; see TableViewer).
 *
 * - Parameter changes (filter / sort / offset / limit, compared by value)
 *   fetch at once and supersede any fetch in flight: a response whose
 *   request is no longer the latest is dropped, so an older page never
 *   lands over a newer one.
 * - `refreshKey` bumps are coalesced: with a fetch in flight the hook only
 *   marks itself dirty and fetches once more when that one lands, so a
 *   burst of writes cannot keep cancelling fetches and starve the grid.
 * - The previous page stays in `page` while a refetch is in flight (no
 *   flash); `loading` says one is. Opening another table drops the page.
 * - A failed fetch keeps the last good page and sets `error` (the
 *   ApiClientError itself, so the filter bar can read `filterErrorPath`);
 *   the next success clears it.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { queryTableRows } from '../api/tablesApi';
import { ApiClientError } from '../api/request';
import type { FilterNode, SortKey, TableRowsPage } from '../api/types';

/** Default page size of the viewer grid (the server cap). */
export const TABLE_ROWS_PAGE_SIZE = 200;

export interface UseTableRowsOptions {
  filter?: FilterNode | null;
  sort?: SortKey[] | null;
  offset?: number;
  limit?: number;
  /** Any change re-queries the current page (coalesced while one is in flight). */
  refreshKey?: number | string;
  /** False: fetch nothing (the page stays as it is). Default true. */
  enabled?: boolean;
}

export interface TableRowsView {
  /** The last page that loaded for this table (kept during refetches). */
  page: TableRowsPage | null;
  /** A fetch is in flight. */
  loading: boolean;
  /** The last fetch failed (cleared by the next success). */
  error: ApiClientError | null;
  /** Re-query now with the current parameters. */
  refresh: () => Promise<void>;
}

interface RowsState {
  tableId: string | null;
  page: TableRowsPage | null;
  error: ApiClientError | null;
}

const EMPTY: RowsState = { tableId: null, page: null, error: null };

export function useTableRows(tableId: string | null, opts: UseTableRowsOptions = {}): TableRowsView {
  const {
    filter = null,
    sort = null,
    offset = 0,
    limit = TABLE_ROWS_PAGE_SIZE,
    refreshKey = 0,
    enabled = true,
  } = opts;
  const paramsKey = JSON.stringify({ filter, sort, offset, limit });

  const [state, setState] = useState<RowsState>(EMPTY);
  const [pending, setPending] = useState(false);

  // The latest parameters, read by run() so a coalesced refetch uses them.
  const paramsRef = useRef({ tableId, filter, sort, offset, limit });
  paramsRef.current = { tableId, filter, sort, offset, limit };
  const refreshKeyRef = useRef(refreshKey);
  refreshKeyRef.current = refreshKey;
  const seqRef = useRef(0);
  const inFlightRef = useRef<number | null>(null);
  const dirtyRef = useRef(false);

  const run = useCallback(async (): Promise<void> => {
    // Loops while refreshKey bumps arrived during the fetch (coalescing).
    for (;;) {
      const p = paramsRef.current;
      if (p.tableId === null) return;
      const seq = ++seqRef.current;
      inFlightRef.current = seq;
      dirtyRef.current = false;
      setPending(true);
      try {
        const page = await queryTableRows(p.tableId, {
          filter: p.filter,
          sort: p.sort,
          offset: p.offset,
          limit: p.limit,
        });
        if (seq === seqRef.current) setState({ tableId: p.tableId, page, error: null });
      } catch (err) {
        if (seq === seqRef.current) {
          const error =
            err instanceof ApiClientError
              ? err
              : new ApiClientError(err instanceof Error ? err.message : 'Failed to load rows');
          setState((prev) =>
            prev.tableId === p.tableId
              ? { ...prev, error }
              : { tableId: p.tableId, page: null, error },
          );
        }
      }
      // Superseded: the newer fetch owns the pending flag and the dirty bit.
      if (inFlightRef.current !== seq) return;
      inFlightRef.current = null;
      if (!dirtyRef.current) {
        setPending(false);
        return;
      }
    }
  }, []);

  const lastRefreshKeyRef = useRef(refreshKey);
  // Parameter (or table) changes: fetch now, superseding any fetch in flight.
  useEffect(() => {
    if (!enabled || tableId === null) {
      // Nothing in flight may land after this.
      seqRef.current += 1;
      inFlightRef.current = null;
      dirtyRef.current = false;
      setPending(false);
      return;
    }
    // This fetch already reflects the current refreshKey; the refresh effect
    // below must not mark it dirty in the same render.
    lastRefreshKeyRef.current = refreshKeyRef.current;
    void run();
  }, [tableId, paramsKey, enabled, run]);

  // refreshKey bumps: coalesce with a fetch in flight.
  useEffect(() => {
    if (lastRefreshKeyRef.current === refreshKey) return;
    lastRefreshKeyRef.current = refreshKey;
    if (!enabled || paramsRef.current.tableId === null) return;
    if (inFlightRef.current !== null) {
      dirtyRef.current = true;
      return;
    }
    void run();
  }, [refreshKey, enabled, run]);

  const current = tableId !== null && state.tableId === tableId ? state : EMPTY;
  return {
    page: current.page,
    loading: tableId !== null && enabled && pending,
    error: current.error,
    refresh: run,
  };
}
