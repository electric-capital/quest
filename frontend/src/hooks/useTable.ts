/**
 * Data layer for the table viewer: one table's detail (row + schema), kept
 * current by the realtime `table_changed` global for THIS table (a copy of
 * useDoc's pattern). An event re-fetches the detail unless the viewer
 * already shows both its `updated_at` and its `schema_version`. The server
 * sends `updated_at: null` (and `schema_version: null`) when the table's
 * existence or the viewer's access changed (a delete, a share added /
 * changed / revoked, its project deleted), which always re-fetches -- that
 * is how a delete or a revoked share turns into `notFound` through the
 * follow-up 404.
 *
 * Every `table_changed` for this id also bumps `changeSeq` and records
 * `lastChange`, re-fetched detail or not, so a rows hook can re-query its
 * grid page on each one (a row write moves `updated_at` but the detail
 * alone does not show the new rows). `table_list_changed` is ignored here:
 * it is broadcast install-wide for writes to everyone-shared tables, and
 * the lists (useTables) are what it is for.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { getTable } from '../api/tablesApi';
import { ApiClientError } from '../api/request';
import type { TableDetail, TableListRow } from '../api/types';
import { persistentWebSocket } from '../services/PersistentWebSocket';

/** The payload of one `table_changed` event for the open table. */
export interface TableChangedEvent {
  table_id: string;
  // Null: existence or access changed (delete, share change, project delete).
  updated_at: string | null;
  schema_version: number | null;
}

export interface TableView {
  table: TableDetail | null;
  // True only until the first response for the current tableId lands (never
  // during a background refresh).
  loading: boolean;
  // A failed load other than 404, while no table is shown. A background
  // refresh failing keeps the loaded table and leaves this null.
  error: ApiClientError | null;
  // The table does not exist or is no longer visible (404, e.g. after a
  // delete or a revoked share); `table` and `error` are null then.
  notFound: boolean;
  refresh: () => Promise<void>;
  // Count of `table_changed` events received for this table since it was
  // opened (0 on open; reset when tableId changes). Depend on it to refetch
  // rows on every change.
  changeSeq: number;
  // The most recent `table_changed` event for this table, or null.
  lastChange: TableChangedEvent | null;
  // Merge a detail returned by the metadata / schema / create / restore
  // endpoints into the view (ignored when older than the shown one).
  applyDetail: (detail: TableDetail) => void;
  // Merge a list row (share routes, the `current` of a stale_update) into
  // the loaded detail, keeping its schema; re-fetches when the row is
  // newer or names a different schema_version.
  applyRow: (row: TableListRow) => void;
}

/** What has been loaded, tagged with the table id it was loaded for. */
interface TableState {
  id: string | null;
  table: TableDetail | null;
  error: ApiClientError | null;
  notFound: boolean;
}

const EMPTY: TableState = { id: null, table: null, error: null, notFound: false };

interface ChangeState {
  id: string | null;
  seq: number;
  last: TableChangedEvent | null;
}

const NO_CHANGES: ChangeState = { id: null, seq: 0, last: null };

export function useTable(tableId: string | null): TableView {
  const [state, setState] = useState<TableState>(EMPTY);
  const [changes, setChanges] = useState<ChangeState>(NO_CHANGES);

  // The committed state, written in step with setState so callbacks and
  // late responses always see the latest one (even before a re-render).
  const stateRef = useRef<TableState>(EMPTY);
  const commit = useCallback((next: TableState) => {
    stateRef.current = next;
    setState(next);
  }, []);
  // Bumped by every fetch and by a tableId change; a response whose number
  // is no longer current is dropped, so a previous table can never land on
  // the one now open, nor an older fetch overwrite a newer one.
  const requestSeqRef = useRef(0);
  // Seq of the fetch in flight, or null.
  const pendingSeqRef = useRef<number | null>(null);

  const refresh = useCallback(async () => {
    if (tableId === null) return;
    const seq = ++requestSeqRef.current;
    pendingSeqRef.current = seq;
    try {
      const detail = await getTable(tableId);
      if (seq !== requestSeqRef.current) return;
      commit({ id: tableId, table: detail, error: null, notFound: false });
    } catch (err) {
      if (seq !== requestSeqRef.current) return;
      if (err instanceof ApiClientError && err.statusCode === 404) {
        commit({ id: tableId, table: null, error: null, notFound: true });
        return;
      }
      const prev = stateRef.current;
      if (prev.id === tableId && prev.table) return;
      const error =
        err instanceof ApiClientError
          ? err
          : new ApiClientError(err instanceof Error ? err.message : 'Failed to load table');
      commit({ id: tableId, table: null, error, notFound: false });
    } finally {
      if (pendingSeqRef.current === seq) pendingSeqRef.current = null;
    }
  }, [tableId, commit]);

  // Load on mount and whenever the table id changes, dropping the previous
  // table (and any in-flight response for it) first.
  useEffect(() => {
    requestSeqRef.current += 1;
    pendingSeqRef.current = null;
    commit(EMPTY);
    setChanges(NO_CHANGES);
    void refresh();
  }, [refresh, commit]);

  const applyDetail = useCallback(
    (detail: TableDetail) => {
      const prev = stateRef.current;
      if (tableId === null || detail.id !== tableId || prev.id !== tableId || !prev.table) return;
      // ISO strings of equal shape: string order is time order. An older
      // response (overtaken by a refresh) must not roll the view back.
      if (detail.updated_at < prev.table.updated_at) return;
      if (detail.schema_version < prev.table.schema_version) return;
      commit({
        ...prev,
        table: {
          ...prev.table,
          ...detail,
          // Only GET /tables/{id} carries it; keep the shown one otherwise.
          last_write_conversation:
            detail.last_write_conversation !== undefined
              ? detail.last_write_conversation
              : prev.table.last_write_conversation,
        },
      });
      // A fetch that started before this write would land with old data,
      // and the writer's conversation link follows last_write_source.
      if (
        pendingSeqRef.current !== null ||
        (detail.last_write_conversation === undefined &&
          detail.last_write_source !== prev.table.last_write_source)
      ) {
        void refresh();
      }
    },
    [tableId, commit, refresh],
  );

  const applyRow = useCallback(
    (row: TableListRow) => {
      const prev = stateRef.current;
      if (tableId === null || row.id !== tableId || prev.id !== tableId || !prev.table) return;
      if (row.updated_at < prev.table.updated_at) return;
      // A row carries no schema: never pair a newer schema_version with the
      // old columns.
      const schemaMoved = row.schema_version !== prev.table.schema_version;
      if (!schemaMoved) {
        commit({ ...prev, table: { ...prev.table, ...row } });
      }
      const newer = row.updated_at > prev.table.updated_at;
      if (newer || schemaMoved || pendingSeqRef.current !== null) void refresh();
    },
    [tableId, commit, refresh],
  );

  useEffect(() => {
    if (tableId === null) return;
    return persistentWebSocket.onGlobalEvent((event) => {
      if (event.type !== 'table_changed' || event.table_id !== tableId) return;
      const change: TableChangedEvent = {
        table_id: tableId,
        updated_at: typeof event.updated_at === 'string' ? event.updated_at : null,
        schema_version: typeof event.schema_version === 'number' ? event.schema_version : null,
      };
      setChanges((prev) =>
        prev.id === tableId
          ? { id: tableId, seq: prev.seq + 1, last: change }
          : { id: tableId, seq: 1, last: change },
      );
      const prev = stateRef.current;
      const shown = prev.id === tableId ? prev.table : null;
      if (
        change.updated_at === null ||
        !shown ||
        change.updated_at !== shown.updated_at ||
        change.schema_version !== shown.schema_version
      ) {
        void refresh();
      }
    });
  }, [tableId, refresh]);

  const current = tableId !== null && state.id === tableId ? state : EMPTY;
  const currentChanges = tableId !== null && changes.id === tableId ? changes : NO_CHANGES;
  return {
    table: current.table,
    loading: tableId !== null && state.id !== tableId,
    error: current.error,
    notFound: current.notFound,
    refresh,
    changeSeq: currentChanges.seq,
    lastChange: currentChanges.last,
    applyDetail,
    applyRow,
  };
}
