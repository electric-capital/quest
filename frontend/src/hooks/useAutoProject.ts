/**
 * Data layer for one AutoProject's detail (title, brief, model,
 * active run, unread seqs), modelled on useTable.
 *
 * Kept current by two realtime globals:
 *
 * - `autoproject_changed` for THIS AutoProject (a run starting or changing status, a
 *   brief write, an answer, a run delete, the AutoProject's delete): bumps
 *   `changeSeq` and re-fetches the detail unless the
 *   shown one already has the event's `event_seq`, `brief_version` and
 *   `active_run` (id and status). A delete sends a last `autoproject_changed` with
 *   a bumped `event_seq` and `active_run: null`; its re-fetch 404s, which
 *   is how an AutoProject deleted elsewhere turns into `notFound`.
 * - `autoproject_list_changed` (rename and the other PATCH fields, archive,
 *   unarchive and delete send only this one): a debounced silent re-fetch.
 *   The event names no AutoProject, so any AutoProject's list change costs one GET.
 *
 * A socket reconnect re-fetches too (globals published while it was down are
 * lost). None of these re-fetch an AutoProject this tab is deleting
 * (`isAutoProjectDeleting`): the delete's own `autoproject_changed` lands before the DELETE
 * returns, and its re-fetch would only 404 on the page about to close.
 *
 * Fetches are coalesced as in useTable: one in flight at a time; an event
 * during it sets a dirty flag and one more fetch runs when it lands, never
 * superseding it (under a burst, superseding would drop every response).
 *
 * The brief panel rule "re-fetch the brief only when brief_version moved"
 * holds without a separate brief fetch: the detail always carries the
 * brief, and consumers key their draft reset on `autoproject.brief_version`, so an
 * unrelated re-fetch (a run status change, a rename) never clobbers an
 * edit in progress.
 *
 * Every loaded detail's unread seqs are merged into the unread store.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { getAutoProject, isAutoProjectDeleting, parseAutoProjectActiveRun } from '../api/autoprojectsApi';
import { ApiClientError } from '../api/request';
import type { AutoProject, AutoProjectActiveRun, AutoProjectBriefState, AutoProjectChangedEvent } from '../api/types';
import { persistentWebSocket } from '../services/PersistentWebSocket';
import { unreadStore } from '../store/unreadStore';
import { AUTOPROJECTS_REFRESH_DEBOUNCE_MS } from './useAutoProjects';

export interface AutoProjectView {
  autoproject: AutoProject | null;
  // True only until the first response for the current autoprojectId lands (never
  // during a background refresh).
  loading: boolean;
  // A failed load other than 404, while no AutoProject is shown. A background
  // refresh failing keeps the loaded AutoProject and leaves this null.
  error: ApiClientError | null;
  // 404: the AutoProject is missing, someone else's, or was deleted; `autoproject` and
  // `error` are null then.
  notFound: boolean;
  refresh: () => Promise<void>;
  // Count of `autoproject_changed` events for this AutoProject since it was opened (0 on
  // open; reset when autoprojectId changes).
  changeSeq: number;
  // Merge an AutoProject returned by PATCH / archive / unarchive (other ids and
  // details older than the shown one are ignored).
  applyDetail: (autoproject: AutoProject) => void;
  // Merge a PUT brief / restore response (ignored when its brief_version is
  // older than the shown one).
  applyBrief: (brief: AutoProjectBriefState) => void;
}

/** What has been loaded, tagged with the AutoProject id it was loaded for. */
interface AutoProjectState {
  id: string | null;
  autoproject: AutoProject | null;
  error: ApiClientError | null;
  notFound: boolean;
}

const EMPTY: AutoProjectState = { id: null, autoproject: null, error: null, notFound: false };

interface ChangeState {
  id: string | null;
  seq: number;
}

const NO_CHANGES: ChangeState = { id: null, seq: 0 };

function sameActiveRun(a: AutoProjectActiveRun | null, b: AutoProjectActiveRun | null): boolean {
  if (a === null || b === null) return a === b;
  return a.id === b.id && a.status === b.status;
}

export function useAutoProject(autoprojectId: string | null): AutoProjectView {
  const [state, setState] = useState<AutoProjectState>(EMPTY);
  const [changes, setChanges] = useState<ChangeState>(NO_CHANGES);

  // The committed state, written in step with setState so callbacks and
  // late responses always see the latest one (even before a re-render).
  const stateRef = useRef<AutoProjectState>(EMPTY);
  const commit = useCallback((next: AutoProjectState) => {
    stateRef.current = next;
    setState(next);
    if (next.autoproject) unreadStore.mergeAutoProject(next.autoproject.id, next.autoproject.unread_seq, next.autoproject.read_seq);
  }, []);
  // Bumped by every fetch and by an autoprojectId change; a response whose number
  // is no longer current is dropped, so a previous AutoProject can never land on
  // the one now open, nor an older fetch overwrite a newer one.
  const requestSeqRef = useRef(0);
  // Seq of the fetch in flight, or null.
  const pendingSeqRef = useRef<number | null>(null);
  // Set by an event that arrives while a fetch is in flight: one more
  // fetch runs when it lands.
  const dirtyRef = useRef(false);
  const refreshRef = useRef<() => Promise<void>>(async () => {});

  const refresh = useCallback(async () => {
    if (autoprojectId === null) return;
    const seq = ++requestSeqRef.current;
    pendingSeqRef.current = seq;
    try {
      const autoproject = await getAutoProject(autoprojectId);
      if (seq !== requestSeqRef.current) return;
      commit({ id: autoprojectId, autoproject, error: null, notFound: false });
    } catch (err) {
      if (seq !== requestSeqRef.current) return;
      if (err instanceof ApiClientError && err.statusCode === 404) {
        commit({ id: autoprojectId, autoproject: null, error: null, notFound: true });
        return;
      }
      const prev = stateRef.current;
      if (prev.id === autoprojectId && (prev.autoproject || prev.notFound)) return;
      const error =
        err instanceof ApiClientError
          ? err
          : new ApiClientError(err instanceof Error ? err.message : 'Failed to load AutoProject');
      commit({ id: autoprojectId, autoproject: null, error, notFound: false });
    } finally {
      if (pendingSeqRef.current === seq) {
        pendingSeqRef.current = null;
        if (dirtyRef.current && seq === requestSeqRef.current) {
          dirtyRef.current = false;
          void refreshRef.current();
        }
      }
    }
  }, [autoprojectId, commit]);
  refreshRef.current = refresh;

  // Re-fetch now, or once the fetch in flight lands (never for an AutoProject this
  // tab is deleting).
  const requestRefresh = useCallback(() => {
    if (autoprojectId === null || isAutoProjectDeleting(autoprojectId)) return;
    if (pendingSeqRef.current !== null) dirtyRef.current = true;
    else void refreshRef.current();
  }, [autoprojectId]);

  // Load on mount and whenever the AutoProject id changes, dropping the previous
  // AutoProject (and any in-flight response for it) first.
  useEffect(() => {
    requestSeqRef.current += 1;
    pendingSeqRef.current = null;
    dirtyRef.current = false;
    commit(EMPTY);
    setChanges(NO_CHANGES);
    void refresh();
  }, [refresh, commit]);

  const applyDetail = useCallback(
    (autoproject: AutoProject) => {
      const prev = stateRef.current;
      if (autoprojectId === null || autoproject.id !== autoprojectId || prev.id !== autoprojectId || !prev.autoproject) return;
      // ISO strings of equal shape: string order is time order. `updated_at`
      // does not move on every change, so an equal one is accepted.
      if (autoproject.updated_at < prev.autoproject.updated_at) return;
      commit({ ...prev, autoproject });
      // A fetch that started before this write would land with old data.
      if (pendingSeqRef.current !== null) void refresh();
    },
    [autoprojectId, commit, refresh],
  );

  const applyBrief = useCallback(
    (brief: AutoProjectBriefState) => {
      const prev = stateRef.current;
      if (autoprojectId === null || prev.id !== autoprojectId || !prev.autoproject) return;
      if (brief.brief_version < prev.autoproject.brief_version) return;
      commit({
        ...prev,
        autoproject: {
          ...prev.autoproject,
          brief: brief.brief,
          brief_version: brief.brief_version,
          brief_updated_at: brief.brief_updated_at,
          brief_source: brief.brief_source,
          brief_run_seq: brief.brief_run_seq,
        },
      });
      if (pendingSeqRef.current !== null) void refresh();
    },
    [autoprojectId, commit, refresh],
  );

  // autoproject_changed for this AutoProject: count it and re-fetch unless already shown.
  useEffect(() => {
    if (autoprojectId === null) return;
    return persistentWebSocket.onGlobalEvent((event) => {
      if (event.type !== 'autoproject_changed' || event.autoproject_id !== autoprojectId) return;
      const change: AutoProjectChangedEvent = {
        autoproject_id: autoprojectId,
        event_seq: typeof event.event_seq === 'number' ? event.event_seq : 0,
        brief_version: typeof event.brief_version === 'number' ? event.brief_version : 0,
        active_run: parseAutoProjectActiveRun(event.active_run),
      };
      setChanges((prev) =>
        prev.id === autoprojectId ? { id: autoprojectId, seq: prev.seq + 1 } : { id: autoprojectId, seq: 1 },
      );
      const prev = stateRef.current;
      const shown = prev.id === autoprojectId ? prev.autoproject : null;
      if (
        !shown ||
        change.event_seq !== shown.event_seq ||
        change.brief_version !== shown.brief_version ||
        !sameActiveRun(change.active_run, shown.active_run)
      ) {
        requestRefresh();
      }
    });
  }, [autoprojectId, requestRefresh]);

  // autoproject_list_changed: debounced silent re-fetch (renames, archive, ...).
  useEffect(() => {
    if (autoprojectId === null) return;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const unsubscribe = persistentWebSocket.onGlobalEvent((event) => {
      if (event.type !== 'autoproject_list_changed') return;
      if (timer !== null) clearTimeout(timer);
      timer = setTimeout(() => {
        timer = null;
        requestRefresh();
      }, AUTOPROJECTS_REFRESH_DEBOUNCE_MS);
    });
    return () => {
      unsubscribe();
      if (timer !== null) clearTimeout(timer);
    };
  }, [autoprojectId, requestRefresh]);

  // Socket reconnect: events missed while it was down are lost.
  useEffect(() => {
    if (autoprojectId === null) return;
    return persistentWebSocket.onReconnect(requestRefresh);
  }, [autoprojectId, requestRefresh]);

  const current = autoprojectId !== null && state.id === autoprojectId ? state : EMPTY;
  const currentChanges = autoprojectId !== null && changes.id === autoprojectId ? changes : NO_CHANGES;
  return {
    autoproject: current.autoproject,
    loading: autoprojectId !== null && state.id !== autoprojectId,
    error: current.error,
    notFound: current.notFound,
    refresh,
    changeSeq: currentChanges.seq,
    applyDetail,
    applyBrief,
  };
}
