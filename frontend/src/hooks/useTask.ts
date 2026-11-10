/**
 * Data layer for one Quest Task's detail (title, instructions, brief, model,
 * active run, unread seqs), modelled on useTable.
 *
 * Kept current by two realtime globals:
 *
 * - `task_changed` for THIS task (a run starting or changing status, a
 *   brief write, an answer, a run delete, the task's delete): bumps
 *   `changeSeq`, records `lastChange`, and re-fetches the detail unless the
 *   shown one already has the event's `event_seq`, `brief_version` and
 *   `active_run` (id and status). A delete sends a last `task_changed` with
 *   a bumped `event_seq` and `active_run: null`; its re-fetch 404s, which
 *   is how a deleted task turns into `notFound`.
 * - `task_list_changed` (rename and the other PATCH fields, archive,
 *   unarchive and delete send only this one): a debounced silent re-fetch.
 *   The event names no task, so any task's list change costs one GET.
 *
 * Fetches are coalesced as in useTable: one in flight at a time; an event
 * during it sets a dirty flag and one more fetch runs when it lands, never
 * superseding it (under a burst, superseding would drop every response).
 *
 * The brief panel rule "re-fetch the brief only when brief_version moved"
 * holds without a separate brief fetch: the detail always carries the
 * brief, and consumers key their draft reset on `task.brief_version`, so an
 * unrelated re-fetch (a run status change, a rename) never clobbers an
 * edit in progress.
 *
 * Every loaded detail's unread seqs are merged into the unread store.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { getTask, parseTaskActiveRun } from '../api/tasksApi';
import { ApiClientError } from '../api/request';
import type { Task, TaskActiveRun, TaskBriefState, TaskChangedEvent } from '../api/types';
import { persistentWebSocket } from '../services/PersistentWebSocket';
import { unreadStore } from '../store/unreadStore';
import { TASKS_REFRESH_DEBOUNCE_MS } from './useTasks';

export interface TaskView {
  task: Task | null;
  // True only until the first response for the current taskId lands (never
  // during a background refresh).
  loading: boolean;
  // A failed load other than 404, while no task is shown. A background
  // refresh failing keeps the loaded task and leaves this null.
  error: ApiClientError | null;
  // 404: the task is missing, someone else's, or was deleted; `task` and
  // `error` are null then.
  notFound: boolean;
  refresh: () => Promise<void>;
  // Count of `task_changed` events for this task since it was opened (0 on
  // open; reset when taskId changes).
  changeSeq: number;
  // The most recent `task_changed` event for this task, or null.
  lastChange: TaskChangedEvent | null;
  // Merge a Task returned by PATCH / archive / unarchive (other ids and
  // details older than the shown one are ignored).
  applyDetail: (task: Task) => void;
  // Merge a PUT brief / restore response (ignored when its brief_version is
  // older than the shown one).
  applyBrief: (brief: TaskBriefState) => void;
}

/** What has been loaded, tagged with the task id it was loaded for. */
interface TaskState {
  id: string | null;
  task: Task | null;
  error: ApiClientError | null;
  notFound: boolean;
}

const EMPTY: TaskState = { id: null, task: null, error: null, notFound: false };

interface ChangeState {
  id: string | null;
  seq: number;
  last: TaskChangedEvent | null;
}

const NO_CHANGES: ChangeState = { id: null, seq: 0, last: null };

function sameActiveRun(a: TaskActiveRun | null, b: TaskActiveRun | null): boolean {
  if (a === null || b === null) return a === b;
  return a.id === b.id && a.status === b.status;
}

export function useTask(taskId: string | null): TaskView {
  const [state, setState] = useState<TaskState>(EMPTY);
  const [changes, setChanges] = useState<ChangeState>(NO_CHANGES);

  // The committed state, written in step with setState so callbacks and
  // late responses always see the latest one (even before a re-render).
  const stateRef = useRef<TaskState>(EMPTY);
  const commit = useCallback((next: TaskState) => {
    stateRef.current = next;
    setState(next);
    if (next.task) unreadStore.mergeTask(next.task.id, next.task.unread_seq, next.task.read_seq);
  }, []);
  // Bumped by every fetch and by a taskId change; a response whose number
  // is no longer current is dropped, so a previous task can never land on
  // the one now open, nor an older fetch overwrite a newer one.
  const requestSeqRef = useRef(0);
  // Seq of the fetch in flight, or null.
  const pendingSeqRef = useRef<number | null>(null);
  // Set by an event that arrives while a fetch is in flight: one more
  // fetch runs when it lands.
  const dirtyRef = useRef(false);
  const refreshRef = useRef<() => Promise<void>>(async () => {});

  const refresh = useCallback(async () => {
    if (taskId === null) return;
    const seq = ++requestSeqRef.current;
    pendingSeqRef.current = seq;
    try {
      const task = await getTask(taskId);
      if (seq !== requestSeqRef.current) return;
      commit({ id: taskId, task, error: null, notFound: false });
    } catch (err) {
      if (seq !== requestSeqRef.current) return;
      if (err instanceof ApiClientError && err.statusCode === 404) {
        commit({ id: taskId, task: null, error: null, notFound: true });
        return;
      }
      const prev = stateRef.current;
      if (prev.id === taskId && (prev.task || prev.notFound)) return;
      const error =
        err instanceof ApiClientError
          ? err
          : new ApiClientError(err instanceof Error ? err.message : 'Failed to load task');
      commit({ id: taskId, task: null, error, notFound: false });
    } finally {
      if (pendingSeqRef.current === seq) {
        pendingSeqRef.current = null;
        if (dirtyRef.current && seq === requestSeqRef.current) {
          dirtyRef.current = false;
          void refreshRef.current();
        }
      }
    }
  }, [taskId, commit]);
  refreshRef.current = refresh;

  // Re-fetch now, or once the fetch in flight lands.
  const requestRefresh = useCallback(() => {
    if (pendingSeqRef.current !== null) dirtyRef.current = true;
    else void refreshRef.current();
  }, []);

  // Load on mount and whenever the task id changes, dropping the previous
  // task (and any in-flight response for it) first.
  useEffect(() => {
    requestSeqRef.current += 1;
    pendingSeqRef.current = null;
    dirtyRef.current = false;
    commit(EMPTY);
    setChanges(NO_CHANGES);
    void refresh();
  }, [refresh, commit]);

  const applyDetail = useCallback(
    (task: Task) => {
      const prev = stateRef.current;
      if (taskId === null || task.id !== taskId || prev.id !== taskId || !prev.task) return;
      // ISO strings of equal shape: string order is time order. `updated_at`
      // does not move on every change, so an equal one is accepted.
      if (task.updated_at < prev.task.updated_at) return;
      commit({ ...prev, task });
      // A fetch that started before this write would land with old data.
      if (pendingSeqRef.current !== null) void refresh();
    },
    [taskId, commit, refresh],
  );

  const applyBrief = useCallback(
    (brief: TaskBriefState) => {
      const prev = stateRef.current;
      if (taskId === null || prev.id !== taskId || !prev.task) return;
      if (brief.brief_version < prev.task.brief_version) return;
      commit({
        ...prev,
        task: {
          ...prev.task,
          brief: brief.brief,
          brief_version: brief.brief_version,
          brief_updated_at: brief.brief_updated_at,
          brief_source: brief.brief_source,
          brief_run_seq: brief.brief_run_seq,
        },
      });
      if (pendingSeqRef.current !== null) void refresh();
    },
    [taskId, commit, refresh],
  );

  // task_changed for this task: count it and re-fetch unless already shown.
  useEffect(() => {
    if (taskId === null) return;
    return persistentWebSocket.onGlobalEvent((event) => {
      if (event.type !== 'task_changed' || event.task_id !== taskId) return;
      const change: TaskChangedEvent = {
        task_id: taskId,
        event_seq: typeof event.event_seq === 'number' ? event.event_seq : 0,
        brief_version: typeof event.brief_version === 'number' ? event.brief_version : 0,
        active_run: parseTaskActiveRun(event.active_run),
      };
      setChanges((prev) =>
        prev.id === taskId
          ? { id: taskId, seq: prev.seq + 1, last: change }
          : { id: taskId, seq: 1, last: change },
      );
      const prev = stateRef.current;
      const shown = prev.id === taskId ? prev.task : null;
      if (
        !shown ||
        change.event_seq !== shown.event_seq ||
        change.brief_version !== shown.brief_version ||
        !sameActiveRun(change.active_run, shown.active_run)
      ) {
        requestRefresh();
      }
    });
  }, [taskId, requestRefresh]);

  // task_list_changed: debounced silent re-fetch (renames, archive, ...).
  useEffect(() => {
    if (taskId === null) return;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const unsubscribe = persistentWebSocket.onGlobalEvent((event) => {
      if (event.type !== 'task_list_changed') return;
      if (timer !== null) clearTimeout(timer);
      timer = setTimeout(() => {
        timer = null;
        requestRefresh();
      }, TASKS_REFRESH_DEBOUNCE_MS);
    });
    return () => {
      unsubscribe();
      if (timer !== null) clearTimeout(timer);
    };
  }, [taskId, requestRefresh]);

  const current = taskId !== null && state.id === taskId ? state : EMPTY;
  const currentChanges = taskId !== null && changes.id === taskId ? changes : NO_CHANGES;
  return {
    task: current.task,
    loading: taskId !== null && state.id !== taskId,
    error: current.error,
    notFound: current.notFound,
    refresh,
    changeSeq: currentChanges.seq,
    lastChange: currentChanges.last,
    applyDetail,
    applyBrief,
  };
}
