/**
 * The task thread's composer state machine (spec 8.2), as a pure function
 * of what the task page knows, so TaskMainColumn only renders its answer:
 *
 * - `archived`: the task is archived; no composer (the page shows the
 *   "This task is archived." notice with Unarchive instead).
 * - `start`: no active run; Send starts the next run. While a start is in
 *   flight (`starting`: the optimistic entry is on screen until its run
 *   shows up in the runs list) the input is `busy`: locked, and no Stop,
 *   since the run conversation is not known yet.
 * - `answer`: the active run is `waiting` with a pending question; Send
 *   answers the OLDEST pending question ("Answering run N" as the label,
 *   ANSWER_PLACEHOLDER in the input). `busy` while the answer is in flight.
 * - `locked`: the active run is `running`, or `waiting` only on approval /
 *   connect cards: the input is `disabled` and Stop is offered on the run's
 *   conversation ("Run N is working" / "Run N is waiting for your
 *   approval").
 *
 * The active run comes from the loaded runs (the newest one that is
 * `running` or `waiting`). The task detail's `active_run` (seq and status
 * only) is the fallback while that run is not loaded yet, unless the loaded
 * runs already show that run finished (the runs list is then newer than the
 * detail). A fallback run has no conversation id, so it locks without Stop.
 */

import type { TaskActiveRun, TaskQuestion, TaskRun } from '../../api/types';

export type TaskThreadComposerMode = 'archived' | 'start' | 'answer' | 'locked';

export interface TaskThreadComposerInput {
  archived: boolean;
  /** The loaded runs, ascending seq. */
  runs: TaskRun[];
  /** The task detail's active run (fallback while the run is not loaded). */
  taskActiveRun: TaskActiveRun | null;
  /** A start is in flight (or its run is not in the runs list yet). */
  starting: boolean;
  /** An answer to the pending question is in flight. */
  answering: boolean;
}

export interface TaskThreadComposerView {
  mode: TaskThreadComposerMode;
  /** Locks the input without Stop (a submit in flight, or a run not loaded yet). */
  busy: boolean;
  /** Locks the input with Stop on `stopConversationId`. */
  disabled: boolean;
  /** The run conversation Stop acts on, when Stop is offered. */
  stopConversationId: string | null;
  placeholder: string;
  /** Short label above the composer ("Answering run N"), or null. */
  label: string | null;
  /** The pending question an answer goes to (mode `answer`). */
  question: TaskQuestion | null;
  /** The active run, when it is loaded. */
  activeRun: TaskRun | null;
}

export const START_PLACEHOLDER = 'What should the next run do?';
export const ANSWER_PLACEHOLDER = 'Type your answer';

export function isActiveRunStatus(status: TaskRun['status']): boolean {
  return status === 'running' || status === 'waiting';
}

/** The newest loaded run that is running or waiting, else null. */
export function findActiveRun(runs: TaskRun[]): TaskRun | null {
  for (let i = runs.length - 1; i >= 0; i -= 1) {
    if (isActiveRunStatus(runs[i].status)) return runs[i];
  }
  return null;
}

/** The oldest pending question of a run, else null. */
export function oldestPendingQuestion(run: TaskRun): TaskQuestion | null {
  return run.questions.find((q) => q.status === 'pending') ?? null;
}

/**
 * What a `waiting` run with no pending question waits on, from its open
 * cards: an approval card wins over a connect card; with neither loaded
 * (e.g. the card row is not on the page yet) the wording stays generic.
 */
export function waitingOnPhrase(run: TaskRun): string {
  if (run.open_requests.some((card) => card.type === 'action_request')) return 'your approval';
  if (run.open_requests.some((card) => card.type === 'connect_request')) return 'you to connect a service';
  return 'you';
}

export function deriveTaskThreadComposer(input: TaskThreadComposerInput): TaskThreadComposerView {
  const base: TaskThreadComposerView = {
    mode: 'start',
    busy: false,
    disabled: false,
    stopConversationId: null,
    placeholder: START_PLACEHOLDER,
    label: null,
    question: null,
    activeRun: null,
  };

  if (input.archived) {
    return { ...base, mode: 'archived', busy: true, placeholder: 'This task is archived.' };
  }

  const activeRun = findActiveRun(input.runs);
  if (activeRun) {
    const question = activeRun.status === 'waiting' ? oldestPendingQuestion(activeRun) : null;
    if (question) {
      const label = `Answering run ${activeRun.seq}`;
      return {
        ...base,
        mode: 'answer',
        busy: input.answering,
        placeholder: ANSWER_PLACEHOLDER,
        label,
        question,
        activeRun,
      };
    }
    return {
      ...base,
      mode: 'locked',
      disabled: true,
      stopConversationId: activeRun.conversation_id,
      placeholder:
        activeRun.status === 'waiting'
          ? `Run ${activeRun.seq} is waiting for ${waitingOnPhrase(activeRun)}`
          : `Run ${activeRun.seq} is working`,
      activeRun,
    };
  }

  // The detail says a run is active, and the loaded runs do not show it
  // finished: it is not loaded yet.
  const fallback = input.taskActiveRun;
  if (fallback && !input.runs.some((run) => run.id === fallback.id)) {
    return {
      ...base,
      mode: 'locked',
      busy: true,
      placeholder:
        fallback.status === 'waiting'
          ? `Run ${fallback.seq} is waiting for you`
          : `Run ${fallback.seq} is working`,
    };
  }

  if (input.starting) {
    return { ...base, busy: true, placeholder: 'Starting the run...' };
  }
  return base;
}
