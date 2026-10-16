/**
 * Data layer for the table History drawer: one table's change log (GET
 * /tables/{id}/changes), newest first, keyset-paged by `before_id` and kept
 * current by the realtime `table_changed` global for THIS table.
 *
 * - The first page loads on mount and whenever the table id changes (the
 *   previous table's list, and any response still in flight for it, is
 *   dropped first). `loadMore` appends the next page from `next_before_id`.
 * - `refresh` re-fetches the loaded window from the top in one request
 *   (capped at the server's 200-entry page; past that the list shrinks back
 *   to 200 and `hasMore` offers the rest again), keeping the list on screen:
 *   a write adds an entry on top, and a restore flips `in_effect` on entries
 *   anywhere in the window, so the window is replaced, never patched. After
 *   a failed first load it is the retry: the list goes back to `loading`,
 *   and a second failure shows the error again.
 * - One call at a time per table. A refresh asked for while a call is in
 *   flight (a window fetch or a page) sets a dirty flag, and one window
 *   fetch runs when that call settles, with the count loaded by then (so a
 *   page that just landed is re-fetched, not wiped). A `loadMore` while a
 *   window fetch is in flight waits for it (`loadingMore` meanwhile) and
 *   then pages on from the boundary the new window ends at. Side by side
 *   they lose pages: a window fetched with the old count wipes a page
 *   appended meanwhile, and a page fetched from a boundary the new window
 *   moved cannot be appended.
 * - Every `table_changed` for this table (row and schema writes, restores,
 *   metadata edits, access changes) schedules a refresh,
 *   TABLE_CHANGES_REFRESH_DEBOUNCE_MS after the last event of a burst but no
 *   later than TABLE_CHANGES_REFRESH_MAX_WAIT_MS after its first, so a
 *   steady stream of writes still re-lists.
 * - `addChange` puts an entry the caller just made (a restore's `change`) on
 *   top at once, ahead of the refresh that brings the rest up to date.
 * - Responses for a table no longer shown are dropped, and a page is
 *   appended only to the list (table and boundary) it was fetched for.
 * - A 403 from any call (other than the `tables_disabled` gate), or a 404
 *   `table_not_found` (the table was deleted or unshared meanwhile), sets
 *   the sticky `forbidden` flag: History is for editors only and the viewer
 *   has lost edit access. Fetching stops until the table id changes.
 *
 * `enabled: false` (e.g. access already known to be lost) fetches nothing.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { TABLE_CHANGES_MAX_PAGE_SIZE, listTableChanges } from '../api/tablesApi';
import { ApiClientError } from '../api/request';
import type { TableChange } from '../api/types';
import { persistentWebSocket } from '../services/PersistentWebSocket';

/** Page size when the caller passes no `limit` (= the server default). */
export const TABLE_CHANGES_DEFAULT_PAGE_SIZE = 50;

/** Quiet period after the last `table_changed` before the re-fetch. */
export const TABLE_CHANGES_REFRESH_DEBOUNCE_MS = 300;

/** Longest a burst of `table_changed` can hold the re-fetch back. */
export const TABLE_CHANGES_REFRESH_MAX_WAIT_MS = 2000;

export interface UseTableChangesOptions {
  /** Page size (clamped to 1..200). */
  limit?: number;
  /** False = no fetch and an empty list. */
  enabled?: boolean;
}

export interface TableChangesList {
  changes: TableChange[];
  // True until the first response for the current table lands, and again
  // while `refresh` retries a failed first load (never during a silent
  // refresh).
  loading: boolean;
  // A page is loading, or waiting for the window fetch in flight.
  loadingMore: boolean;
  // Message of a failed first load; cleared by the next successful fetch.
  error: string | null;
  // Message of the last failed `loadMore` (the list stays); cleared by the
  // next attempt.
  loadMoreError: string | null;
  // A history call answered 403 (or 404 `table_not_found`): the viewer is
  // no longer an editor.
  forbidden: boolean;
  hasMore: boolean;
  loadMore: () => void;
  // Re-fetch the loaded window in one request, keeping the list on screen
  // (deferred, and resolved at once, while another call is in flight).
  refresh: () => Promise<void>;
  // Put an entry the caller just made on top of the list.
  addChange: (change: TableChange) => void;
}

/** What has been loaded, tagged with the table id it was loaded for. */
interface ListState {
  key: string | null;
  changes: TableChange[];
  nextBeforeId: number | null;
  error: string | null;
}

const EMPTY: ListState = { key: null, changes: [], nextBeforeId: null, error: null };

/**
 * The viewer can no longer use History: a 403 from a history route (lost
 * edit access; the feature gate is not it), or a 404 `table_not_found` (the
 * table was deleted or unshared while open).
 */
export function isLostTableAccess(err: unknown): boolean {
  if (!(err instanceof ApiClientError)) return false;
  if (err.statusCode === 403) return err.errorCode !== 'tables_disabled';
  return err.statusCode === 404 && err.errorCode === 'table_not_found';
}

function message(err: unknown, fallback: string): string {
  return err instanceof ApiClientError && err.message ? err.message : fallback;
}

export function useTableChanges(
  tableId: string | null,
  { limit = TABLE_CHANGES_DEFAULT_PAGE_SIZE, enabled = true }: UseTableChangesOptions = {},
): TableChangesList {
  const pageSize = Math.min(TABLE_CHANGES_MAX_PAGE_SIZE, Math.max(1, Math.floor(limit)));
  const [forbiddenFor, setForbiddenFor] = useState<string | null>(null);
  const forbidden = tableId !== null && forbiddenFor === tableId;
  const key = enabled && tableId !== null && !forbidden ? tableId : null;

  const [state, setState] = useState<ListState>(EMPTY);
  const [loadingMore, setLoadingMore] = useState(false);
  const [loadMoreError, setLoadMoreError] = useState<string | null>(null);

  // The committed list, written in step with setState so callbacks and late
  // responses always see the latest one (even before a re-render).
  const stateRef = useRef<ListState>(EMPTY);
  const commit = useCallback((next: ListState) => {
    stateRef.current = next;
    setState(next);
  }, []);
  // Bumped by a table switch and by lost access: a response from an older
  // generation is dropped.
  const genRef = useRef(0);
  // The call in flight for the current generation, and what is owed once it
  // settles: a window fetch (dirty), a page (see the header comment).
  const busyRef = useRef<'window' | 'page' | null>(null);
  const dirtyRef = useRef(false);
  const pageOwedRef = useRef(false);
  // Latest closures, for calls chained from a settling one.
  const refreshWindowRef = useRef<() => Promise<void>>(async () => {});
  const fetchPageRef = useRef<() => void>(() => {});

  const loseAccess = useCallback((lostKey: string) => {
    genRef.current += 1;
    setForbiddenFor(lostKey);
  }, []);

  // A call settled: run what was asked for meanwhile, a page first (someone
  // clicked for it; the window fetch then covers it too).
  const settle = useCallback(() => {
    busyRef.current = null;
    if (pageOwedRef.current) {
      pageOwedRef.current = false;
      fetchPageRef.current();
    } else if (dirtyRef.current) {
      dirtyRef.current = false;
      void refreshWindowRef.current();
    }
  }, []);

  // Fetch `count` entries from the top and replace the list with them.
  const fetchWindow = useCallback(
    async (count: number) => {
      if (key === null) return;
      if (busyRef.current !== null) {
        dirtyRef.current = true;
        return;
      }
      busyRef.current = 'window';
      const gen = genRef.current;
      try {
        const response = await listTableChanges(key, { limit: count });
        if (gen !== genRef.current) return;
        commit({
          key,
          changes: response.changes,
          nextBeforeId: response.has_more ? response.next_before_id : null,
          error: null,
        });
      } catch (err) {
        if (gen !== genRef.current) return;
        if (isLostTableAccess(err)) {
          loseAccess(key);
          return;
        }
        // A background refresh failing keeps what is on screen.
        if (stateRef.current.key === key) return;
        commit({
          key,
          changes: [],
          nextBeforeId: null,
          error: message(err, 'Failed to load the history.'),
        });
      } finally {
        if (gen === genRef.current) settle();
      }
    },
    [key, commit, loseAccess, settle],
  );

  // First page on mount and whenever the table changes.
  useEffect(() => {
    genRef.current += 1;
    busyRef.current = null;
    dirtyRef.current = false;
    pageOwedRef.current = false;
    commit(EMPTY);
    setLoadingMore(false);
    setLoadMoreError(null);
    void fetchWindow(pageSize);
  }, [fetchWindow, pageSize, commit]);

  // The loaded window again (silent: the list stays on screen).
  const refreshWindow = useCallback(async () => {
    if (key === null) return;
    const loaded = stateRef.current.key === key ? stateRef.current.changes.length : 0;
    await fetchWindow(Math.min(TABLE_CHANGES_MAX_PAGE_SIZE, Math.max(pageSize, loaded)));
  }, [fetchWindow, key, pageSize]);
  refreshWindowRef.current = refreshWindow;

  const refresh = useCallback(async () => {
    if (key === null) return;
    // Retrying a failed first load: back to `loading`, so a second failure
    // lands as a fresh error.
    if (stateRef.current.key === key && stateRef.current.error !== null) commit(EMPTY);
    await refreshWindow();
  }, [key, commit, refreshWindow]);

  const fetchPage = useCallback(() => {
    const prev = stateRef.current;
    const beforeId = key !== null && prev.key === key ? prev.nextBeforeId : null;
    if (key === null || beforeId === null) {
      // The window fetch it waited for reached the end of the log.
      setLoadingMore(false);
      settle();
      return;
    }
    busyRef.current = 'page';
    const gen = genRef.current;
    void (async () => {
      try {
        const response = await listTableChanges(key, { beforeId, limit: pageSize });
        if (gen !== genRef.current) return;
        const cur = stateRef.current;
        // Only valid for the list and boundary it extends.
        if (cur.key !== key || cur.nextBeforeId !== beforeId) return;
        const seen = new Set(cur.changes.map((c) => c.id));
        commit({
          ...cur,
          changes: [...cur.changes, ...response.changes.filter((c) => !seen.has(c.id))],
          nextBeforeId: response.has_more ? response.next_before_id : null,
        });
      } catch (err) {
        if (gen !== genRef.current) return;
        if (isLostTableAccess(err)) loseAccess(key);
        else setLoadMoreError(message(err, 'Failed to load more changes.'));
      } finally {
        if (gen === genRef.current) {
          setLoadingMore(false);
          settle();
        }
      }
    })();
  }, [key, pageSize, commit, loseAccess, settle]);
  fetchPageRef.current = fetchPage;

  const loadMore = useCallback(() => {
    const prev = stateRef.current;
    if (key === null || prev.key !== key || prev.nextBeforeId === null) return;
    if (busyRef.current === 'page' || pageOwedRef.current) return;
    setLoadingMore(true);
    setLoadMoreError(null);
    if (busyRef.current !== null) pageOwedRef.current = true;
    else fetchPage();
  }, [key, fetchPage]);

  const addChange = useCallback(
    (change: TableChange) => {
      const cur = stateRef.current;
      if (key === null || cur.key !== key || cur.error !== null) return;
      if (cur.changes.length > 0 && change.id <= cur.changes[0].id) return;
      commit({ ...cur, changes: [change, ...cur.changes] });
    },
    [key, commit],
  );

  // Realtime: this table's `table_changed`, trailing-debounced with a max wait.
  useEffect(() => {
    if (key === null) return;
    let timer: ReturnType<typeof setTimeout> | null = null;
    let burstStart: number | null = null;
    const unsubscribe = persistentWebSocket.onGlobalEvent((event) => {
      if (event.type !== 'table_changed' || event.table_id !== key) return;
      const now = Date.now();
      if (burstStart === null) burstStart = now;
      if (timer !== null) clearTimeout(timer);
      const wait = Math.min(
        TABLE_CHANGES_REFRESH_DEBOUNCE_MS,
        Math.max(0, burstStart + TABLE_CHANGES_REFRESH_MAX_WAIT_MS - now),
      );
      timer = setTimeout(() => {
        timer = null;
        burstStart = null;
        void refreshWindowRef.current();
      }, wait);
    });
    return () => {
      unsubscribe();
      if (timer !== null) clearTimeout(timer);
    };
  }, [key]);

  const current = key !== null && state.key === key ? state : EMPTY;
  return {
    changes: current.changes,
    loading: key !== null && state.key !== key,
    loadingMore: key !== null && loadingMore,
    error: current.error,
    loadMoreError: key !== null ? loadMoreError : null,
    forbidden,
    hasMore: current.nextBeforeId !== null,
    loadMore,
    refresh,
    addChange,
  };
}
