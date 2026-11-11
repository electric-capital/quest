/**
 * Data layer of the task page's run panel: the run of task `taskId` whose
 * conversation is `conversationId`, kept current while the view is open.
 *
 * There is no by-conversation run route, so the first lookup pages the
 * task's runs (newest first, the largest page the server allows) until the
 * run with that conversation shows up or the pages end. Once found, a
 * refresh reads just that run back (`before_seq = seq + 1, limit = 1`);
 * a refresh that no longer finds it (the run was deleted) turns into
 * `notFound`, as does a conversation that is not a run of this task
 * (another task's run, a plain chat, a typo) and a missing or foreign task
 * (404).
 *
 * The caller passes useTask's `changeSeq`: every `task_changed` of the task
 * (run status changes, answers, deletes) re-reads the run, so its status
 * moves live. A socket reconnect re-reads it too (useTask re-fetches then
 * but does not bump `changeSeq`). Neither re-reads a task this tab is
 * deleting (`isTaskDeleting`). Fetches coalesce as in useTask: one in
 * flight; a change during it sets a dirty flag and one more fetch runs when
 * it lands.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { isTaskDeleting, listTaskRuns, TASK_RUNS_MAX_PAGE_SIZE } from '../api/tasksApi';
import { ApiClientError } from '../api/request';
import type { TaskRun } from '../api/types';
import { persistentWebSocket } from '../services/PersistentWebSocket';

export interface TaskRunLookup {
  run: TaskRun | null;
  // True only until the first lookup for the current task and conversation
  // landed (never during a background refresh).
  loading: boolean;
  // The conversation is not a run of this task, the run was deleted, or the
  // task is missing or someone else's.
  notFound: boolean;
  // A failed lookup other than 404, while no run is shown.
  error: ApiClientError | null;
  refresh: () => Promise<void>;
}

interface RunState {
  key: string | null;
  run: TaskRun | null;
  notFound: boolean;
  error: ApiClientError | null;
}

const EMPTY: RunState = { key: null, run: null, notFound: false, error: null };

function lookupKey(taskId: string, conversationId: string): string {
  return `${taskId}\n${conversationId}`;
}

/** The run of `taskId` with `conversationId`, or null when it has none. */
async function findRun(
  taskId: string,
  conversationId: string,
  knownSeq: number | null,
): Promise<TaskRun | null> {
  if (knownSeq !== null) {
    const page = await listTaskRuns(taskId, { beforeSeq: knownSeq + 1, limit: 1 });
    const run = page.runs[0];
    return run && run.conversation_id === conversationId ? run : null;
  }
  let beforeSeq: number | null = null;
  for (;;) {
    const page = await listTaskRuns(taskId, { beforeSeq, limit: TASK_RUNS_MAX_PAGE_SIZE });
    const run = page.runs.find((r) => r.conversation_id === conversationId);
    if (run) return run;
    if (page.next_before_seq === null) return null;
    beforeSeq = page.next_before_seq;
  }
}

export function useTaskRun(taskId: string, conversationId: string, changeSeq: number): TaskRunLookup {
  const key = lookupKey(taskId, conversationId);
  const [state, setState] = useState<RunState>(EMPTY);

  const stateRef = useRef<RunState>(EMPTY);
  const commit = useCallback((next: RunState) => {
    stateRef.current = next;
    setState(next);
  }, []);
  const requestSeqRef = useRef(0);
  const pendingSeqRef = useRef<number | null>(null);
  const dirtyRef = useRef(false);
  const refreshRef = useRef<() => Promise<void>>(async () => {});

  const refresh = useCallback(async () => {
    const seq = ++requestSeqRef.current;
    pendingSeqRef.current = seq;
    const prev = stateRef.current;
    const knownSeq = prev.key === key && prev.run ? prev.run.seq : null;
    try {
      const run = await findRun(taskId, conversationId, knownSeq);
      if (seq !== requestSeqRef.current) return;
      commit({ key, run, notFound: run === null, error: null });
    } catch (err) {
      if (seq !== requestSeqRef.current) return;
      if (err instanceof ApiClientError && err.statusCode === 404) {
        commit({ key, run: null, notFound: true, error: null });
        return;
      }
      const shown = stateRef.current;
      if (shown.key === key && (shown.run || shown.notFound)) return;
      const error =
        err instanceof ApiClientError
          ? err
          : new ApiClientError(err instanceof Error ? err.message : 'Failed to load the run');
      commit({ key, run: null, notFound: false, error });
    } finally {
      if (pendingSeqRef.current === seq) {
        pendingSeqRef.current = null;
        if (dirtyRef.current && seq === requestSeqRef.current) {
          dirtyRef.current = false;
          void refreshRef.current();
        }
      }
    }
  }, [key, taskId, conversationId, commit]);
  refreshRef.current = refresh;

  // Look up on mount and whenever the task or conversation changes,
  // dropping the previous run (and any in-flight response for it) first.
  useEffect(() => {
    requestSeqRef.current += 1;
    pendingSeqRef.current = null;
    dirtyRef.current = false;
    commit(EMPTY);
    void refresh();
  }, [refresh, commit]);

  // Re-read the run now, or once the fetch in flight lands (never for a
  // task this tab is deleting).
  const requestRefresh = useCallback(() => {
    if (isTaskDeleting(taskId)) return;
    if (pendingSeqRef.current !== null) dirtyRef.current = true;
    else void refreshRef.current();
  }, [taskId]);

  // A task_changed of this task (useTask resets changeSeq to 0 on a task
  // change, so a new requestRefresh alone never re-reads here).
  useEffect(() => {
    if (changeSeq === 0) return;
    requestRefresh();
  }, [changeSeq, requestRefresh]);

  // Socket reconnect: changes missed while it was down are lost.
  useEffect(() => persistentWebSocket.onReconnect(requestRefresh), [requestRefresh]);

  const current = state.key === key ? state : EMPTY;
  return {
    run: current.run,
    loading: state.key !== key,
    notFound: current.notFound,
    error: current.error,
    refresh,
  };
}
