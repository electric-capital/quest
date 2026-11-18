/**
 * Data layer for an AutoProject's thread: its runs, paged newest first by the
 * server (GET /autoprojects/{id}/runs) and exposed in ASCENDING seq order (display
 * order, oldest at the top).
 *
 * The newest page loads on mount / autoprojectId change; `loadOlder` fetches the
 * page below the oldest loaded run (`before_seq = next_before_seq`) and
 * prepends it. Every `autoproject_changed` for THIS AutoProject (a run starting or
 * changing status, a question asked or answered, a run deleted) and
 * `refresh()` re-fetch the newest page and MERGE it into what is loaded:
 *
 * - Loaded runs with `seq >=` the fetched page's lowest seq are replaced by
 *   the page, so a run deleted inside that window disappears and status
 *   changes land; loaded runs older than that are kept.
 * - The older-page cursor is kept when older runs are kept; otherwise the
 *   page's own `next_before_seq` is taken.
 * - When the page reaches the bottom of the thread (`next_before_seq`
 *   null) it IS the whole thread and replaces the list. When it does not
 *   overlap or touch the loaded runs (its lowest seq is above the newest
 *   loaded seq + 1 while older runs exist), a gap could hide runs, so the
 *   page replaces the list with its cursor (older pages load again on
 *   scroll).
 *
 * A run deleted below the newest page is not seen by the merge; the caller
 * drops it with `removeRun` after its successful deleteAutoProjectRun.
 *
 * A socket reconnect re-fetches the newest page too (globals published while
 * it was down are lost). Neither an event nor a reconnect re-fetches an AutoProject
 * this tab is deleting (`isAutoProjectDeleting`): it would only 404.
 *
 * Fetches of the newest page are coalesced as in useTable: an event during
 * a fetch sets a dirty flag and one more fetch runs when it lands. A
 * superseded response, or one for an AutoProject no longer open, is dropped; an
 * older page whose list was replaced meanwhile is dropped too. A 404 (the
 * AutoProject was deleted, or is not the user's) sets `notFound`.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import {
  AUTOPROJECT_RUNS_MAX_PAGE_SIZE,
  AUTOPROJECT_RUNS_PAGE_SIZE,
  isAutoProjectDeleting,
  listAutoProjectRuns,
} from '../api/autoprojectsApi';
import { ApiClientError } from '../api/request';
import type { AutoProjectRun, AutoProjectRunsPage } from '../api/types';
import { persistentWebSocket } from '../services/PersistentWebSocket';

export interface AutoProjectRunsView {
  // ASCENDING seq (display order: oldest at the top).
  runs: AutoProjectRun[];
  // True only until the first response for the current autoprojectId lands.
  loading: boolean;
  // A failed first load other than 404; a failed background refresh keeps
  // the runs on screen and leaves this null.
  error: ApiClientError | null;
  // 404: the AutoProject is missing, someone else's, or was deleted.
  notFound: boolean;
  hasOlder: boolean;
  loadingOlder: boolean;
  loadOlder: () => void;
  // Re-fetch the newest page and merge it.
  refresh: () => Promise<void>;
  // Drop a run locally after a successful deleteAutoProjectRun.
  removeRun: (runId: string) => void;
}

/** What has been loaded, tagged with the AutoProject id it was loaded for. */
interface RunsState {
  id: string | null;
  // Ascending seq.
  runs: AutoProjectRun[];
  // `before_seq` of the next older page, or null at the bottom.
  nextBeforeSeq: number | null;
  error: ApiClientError | null;
  notFound: boolean;
  // Bumped whenever the list is replaced rather than merged or extended; an
  // older page fetched for an earlier generation is dropped.
  generation: number;
}

const EMPTY: RunsState = {
  id: null,
  runs: [],
  nextBeforeSeq: null,
  error: null,
  notFound: false,
  generation: 0,
};

/** A page's runs in ascending seq order (the server sends newest first). */
function ascending(page: AutoProjectRunsPage): AutoProjectRun[] {
  return [...page.runs].sort((a, b) => a.seq - b.seq);
}

/**
 * Merge a freshly fetched newest page into the loaded runs (see the module
 * comment). Returns the new runs and cursor, and whether the list was
 * replaced.
 */
function mergeNewestPage(
  prev: RunsState,
  page: AutoProjectRunsPage,
): { runs: AutoProjectRun[]; nextBeforeSeq: number | null; replaced: boolean } {
  const fetched = ascending(page);
  const replace = { runs: fetched, nextBeforeSeq: page.next_before_seq, replaced: true };
  // The page reaches the bottom: it is the whole thread.
  if (page.next_before_seq === null || fetched.length === 0) return replace;
  if (prev.runs.length === 0) return replace;
  const lowest = fetched[0].seq;
  const newestLoaded = prev.runs[prev.runs.length - 1].seq;
  // Runs between the loaded ones and the page could be missing.
  if (lowest > newestLoaded + 1) return replace;
  const kept = prev.runs.filter((run) => run.seq < lowest);
  return {
    runs: [...kept, ...fetched],
    nextBeforeSeq: kept.length > 0 ? prev.nextBeforeSeq : page.next_before_seq,
    replaced: false,
  };
}

export function useAutoProjectRuns(
  autoprojectId: string | null,
  opts: { pageSize?: number } = {},
): AutoProjectRunsView {
  const pageSize = Math.min(
    AUTOPROJECT_RUNS_MAX_PAGE_SIZE,
    Math.max(1, Math.floor(opts.pageSize ?? AUTOPROJECT_RUNS_PAGE_SIZE)),
  );

  const [state, setState] = useState<RunsState>(EMPTY);
  const [loadingOlder, setLoadingOlder] = useState(false);

  // The committed state, written in step with setState so callbacks and
  // late responses always see the latest one (even before a re-render).
  const stateRef = useRef<RunsState>(EMPTY);
  const commit = useCallback((next: RunsState) => {
    stateRef.current = next;
    setState(next);
  }, []);
  // Bumped by every newest-page fetch and by an autoprojectId change; a response
  // whose number is no longer current is dropped.
  const requestSeqRef = useRef(0);
  // Seq of the newest-page fetch in flight, or null.
  const pendingSeqRef = useRef<number | null>(null);
  // Set by an event that arrives while a fetch is in flight: one more fetch
  // runs when it lands, rather than superseding it.
  const dirtyRef = useRef(false);
  const refreshRef = useRef<() => Promise<void>>(async () => {});
  // Same idea for loadOlder, so a dropped page cannot clear a newer one's flag.
  const loadOlderSeqRef = useRef(0);

  const refresh = useCallback(async () => {
    if (autoprojectId === null) return;
    const seq = ++requestSeqRef.current;
    pendingSeqRef.current = seq;
    try {
      const page = await listAutoProjectRuns(autoprojectId, { limit: pageSize });
      if (seq !== requestSeqRef.current) return;
      const prev = stateRef.current;
      if (prev.id !== autoprojectId || prev.notFound) {
        commit({
          id: autoprojectId,
          runs: ascending(page),
          nextBeforeSeq: page.next_before_seq,
          error: null,
          notFound: false,
          generation: prev.generation + 1,
        });
        return;
      }
      const merged = mergeNewestPage(prev, page);
      commit({
        id: autoprojectId,
        runs: merged.runs,
        nextBeforeSeq: merged.nextBeforeSeq,
        error: null,
        notFound: false,
        generation: merged.replaced ? prev.generation + 1 : prev.generation,
      });
    } catch (err) {
      if (seq !== requestSeqRef.current) return;
      const prev = stateRef.current;
      if (err instanceof ApiClientError && err.statusCode === 404) {
        commit({ ...EMPTY, id: autoprojectId, notFound: true, generation: prev.generation + 1 });
        return;
      }
      // A background refresh failing keeps what is on screen.
      if (prev.id === autoprojectId && !prev.error) return;
      const error =
        err instanceof ApiClientError
          ? err
          : new ApiClientError(err instanceof Error ? err.message : 'Failed to load the AutoProject updates');
      commit({ ...EMPTY, id: autoprojectId, error, generation: prev.generation + 1 });
    } finally {
      if (pendingSeqRef.current === seq) {
        pendingSeqRef.current = null;
        if (dirtyRef.current && seq === requestSeqRef.current) {
          dirtyRef.current = false;
          void refreshRef.current();
        }
      }
    }
  }, [autoprojectId, pageSize, commit]);
  refreshRef.current = refresh;

  // Newest page on mount and whenever the AutoProject (or page size) changes,
  // dropping the previous thread (and any in-flight response for it) first.
  useEffect(() => {
    requestSeqRef.current += 1;
    loadOlderSeqRef.current += 1;
    pendingSeqRef.current = null;
    dirtyRef.current = false;
    commit({ ...EMPTY, generation: stateRef.current.generation + 1 });
    setLoadingOlder(false);
    void refresh();
  }, [refresh, commit]);

  const loadOlder = useCallback(() => {
    const prev = stateRef.current;
    const cursor = prev.id === autoprojectId ? prev.nextBeforeSeq : null;
    if (autoprojectId === null || cursor === null || loadingOlder) return;
    const generation = prev.generation;
    const token = ++loadOlderSeqRef.current;
    setLoadingOlder(true);
    void (async () => {
      try {
        const page = await listAutoProjectRuns(autoprojectId, { beforeSeq: cursor, limit: pageSize });
        const current = stateRef.current;
        // The list was replaced (or the AutoProject changed) meanwhile, or a merge
        // moved the cursor: the page no longer extends what is shown.
        if (
          current.id !== autoprojectId ||
          current.generation !== generation ||
          current.nextBeforeSeq !== cursor
        ) {
          return;
        }
        const seen = new Set(current.runs.map((run) => run.id));
        commit({
          ...current,
          runs: [...ascending(page).filter((run) => !seen.has(run.id)), ...current.runs],
          nextBeforeSeq: page.next_before_seq,
        });
      } catch {
        // Keep the list; the next scroll retries.
      } finally {
        if (token === loadOlderSeqRef.current) setLoadingOlder(false);
      }
    })();
  }, [autoprojectId, pageSize, loadingOlder, commit]);

  const removeRun = useCallback(
    (runId: string) => {
      const prev = stateRef.current;
      if (prev.id !== autoprojectId || !prev.runs.some((run) => run.id === runId)) return;
      commit({ ...prev, runs: prev.runs.filter((run) => run.id !== runId) });
    },
    [autoprojectId, commit],
  );

  // autoproject_changed for this AutoProject, or a socket reconnect: re-fetch the newest
  // page (coalesced; never for an AutoProject this tab is deleting).
  useEffect(() => {
    if (autoprojectId === null) return;
    const requestRefresh = () => {
      if (isAutoProjectDeleting(autoprojectId)) return;
      if (pendingSeqRef.current !== null) dirtyRef.current = true;
      else void refreshRef.current();
    };
    const unsubscribeEvents = persistentWebSocket.onGlobalEvent((event) => {
      if (event.type !== 'autoproject_changed' || event.autoproject_id !== autoprojectId) return;
      requestRefresh();
    });
    const unsubscribeReconnect = persistentWebSocket.onReconnect(requestRefresh);
    return () => {
      unsubscribeEvents();
      unsubscribeReconnect();
    };
  }, [autoprojectId]);

  const current = autoprojectId !== null && state.id === autoprojectId ? state : EMPTY;
  return {
    runs: current.runs,
    loading: autoprojectId !== null && state.id !== autoprojectId,
    error: current.error,
    notFound: current.notFound,
    hasOlder: current.nextBeforeSeq !== null,
    loadingOlder: autoprojectId !== null && loadingOlder,
    loadOlder,
    refresh,
    removeRun,
  };
}
