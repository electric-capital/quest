/**
 * Data layer for a Quest Tasks list: the user's unarchived tasks (the
 * sidebar) or the archived ones (`archived: true`), in server order (newest
 * activity first). GET /tasks is not paged, so this is useTables without
 * the paging: the list on mount / whenever the arguments change, `refresh`
 * silently re-fetches it, and the `task_list_changed` realtime global
 * (create, rename, archive, delete, run start and status changes, a read
 * acknowledgement that moved) triggers that refresh.
 *
 * A closed gate (`enabled: false`) fetches nothing and shows an empty list;
 * a superseded response is dropped; a burst of `task_list_changed` costs
 * one re-fetch, TASKS_REFRESH_DEBOUNCE_MS after the last event; a failed
 * background refresh keeps the list on screen. Every row's unread seqs are
 * merged into the unread store, which is how a task's dot follows a run
 * (there is no task unread event).
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { listTasks } from '../api/tasksApi';
import { ApiClientError } from '../api/request';
import type { TaskListRow } from '../api/types';
import { persistentWebSocket } from '../services/PersistentWebSocket';
import { unreadStore } from '../store/unreadStore';

/** Quiet period after the last task event before the re-fetch (lists and detail). */
export const TASKS_REFRESH_DEBOUNCE_MS = 300;

export interface UseTasksOptions {
  /** The archived list instead of the active one. */
  archived?: boolean;
  /** False (gate closed, nothing to show) = no fetch and an empty list. */
  enabled?: boolean;
}

export interface TasksList {
  tasks: TaskListRow[];
  // True only until the first response for the current arguments lands
  // (never during a silent refresh).
  loading: boolean;
  // Message of a failed first load; cleared by the next successful fetch.
  error: string | null;
  // Re-fetch the list, keeping it on screen.
  refresh: () => Promise<void>;
}

/** What has been loaded, tagged with the arguments it was loaded for. */
interface ListState {
  key: string | null;
  tasks: TaskListRow[];
  error: string | null;
}

const EMPTY: ListState = { key: null, tasks: [], error: null };

export function useTasks({ archived = false, enabled = true }: UseTasksOptions = {}): TasksList {
  // Identity of the list being shown; null while disabled.
  const key = enabled ? (archived ? 'archived' : 'active') : null;

  const [state, setState] = useState<ListState>(EMPTY);

  // The committed list, written in step with setState so late responses
  // always see the latest one (even before a re-render).
  const stateRef = useRef<ListState>(EMPTY);
  const commit = useCallback((next: ListState) => {
    stateRef.current = next;
    setState(next);
  }, []);
  // Bumped by every fetch and by an argument change; a response whose
  // number is no longer current is dropped.
  const requestSeqRef = useRef(0);

  const refresh = useCallback(async () => {
    if (key === null) return;
    const seq = ++requestSeqRef.current;
    try {
      const response = await listTasks({ archived });
      if (seq !== requestSeqRef.current) return;
      for (const row of response.tasks) {
        unreadStore.mergeTask(row.id, row.unread_seq, row.read_seq);
      }
      commit({ key, tasks: response.tasks, error: null });
    } catch (err) {
      if (seq !== requestSeqRef.current) return;
      // A background refresh failing keeps what is on screen; only a list
      // that never loaded for these arguments shows the error.
      if (stateRef.current.key === key) return;
      commit({
        key,
        tasks: [],
        error: err instanceof ApiClientError ? err.message : 'Failed to load tasks',
      });
    }
  }, [key, archived, commit]);

  // Load on mount and whenever the arguments change: drop the old list (and
  // any in-flight response for it) before fetching.
  useEffect(() => {
    requestSeqRef.current += 1;
    commit(EMPTY);
    void refresh();
  }, [refresh, commit]);

  // Realtime: trailing-debounced so a burst costs one re-fetch.
  useEffect(() => {
    if (key === null) return;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const unsubscribe = persistentWebSocket.onGlobalEvent((event) => {
      if (event.type !== 'task_list_changed') return;
      if (timer !== null) clearTimeout(timer);
      timer = setTimeout(() => {
        timer = null;
        void refresh();
      }, TASKS_REFRESH_DEBOUNCE_MS);
    });
    return () => {
      unsubscribe();
      if (timer !== null) clearTimeout(timer);
    };
  }, [key, refresh]);

  const current = key !== null && state.key === key ? state : EMPTY;
  return {
    tasks: current.tasks,
    loading: key !== null && state.key !== key,
    error: current.error,
    refresh,
  };
}
