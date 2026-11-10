/**
 * API client for Quest Tasks (/app/api/tasks; see the module docstrings of
 * chat/tasks/routes.py, chat/tasks/thread.py for the response shapes and
 * chat/tasks/http.py for the error bodies).
 *
 * Every call goes through the shared request wrapper, so failures reject
 * with an ApiClientError carrying the server's error code (`errorCode`) and
 * the whole parsed body (`details`). Every task route checks the gate first
 * (403 `tasks_disabled`), then ownership (404 `task_not_found`, the same
 * body for a missing task and someone else's). Task errors are FastAPI
 * envelopes, `{detail: {error, message, ...extras}}`; the extras (the
 * `current` brief of a 409 stale_brief, the `active_run` of a 409
 * task_run_active, the `cap` / `actual` of a 400 brief_too_long) are pulled
 * out by the accessors at the bottom.
 *
 * A run is not started here: POST /tasks/{id}/runs only creates the run's
 * conversation, and the run starts with the first `send_message` on it
 * (services/taskRuns.ts does both).
 */

import { API_BASE_URL } from './config';
import { ApiClientError, apiDelete, apiGet, apiPatch, apiPost, apiPut } from './request';
import type {
  CreateTaskRequest,
  ListTasksResponse,
  Task,
  TaskActiveRun,
  TaskBriefState,
  TaskBriefVersion,
  TaskBriefVersionDetail,
  TaskQuestion,
  TaskReadState,
  TaskRunsPage,
  UpdateTaskRequest,
} from './types';

/** Feature-gate name; the UI shows tasks only when `enabledFeatures` has it. */
export const TASKS_FEATURE = 'tasks';

/** Server default page size of GET /tasks/{id}/runs. */
export const TASK_RUNS_PAGE_SIZE = 20;

/** Server cap on a runs page (`limit` must be 1..100). */
export const TASK_RUNS_MAX_PAGE_SIZE = 100;

// Hand-mirrored caps of chat/tasks/constants.py, for the editors' counters.
// The server enforces them; keep these in step when one changes there.
export const TASK_TITLE_MAX_CHARS = 120;
export const TASK_INSTRUCTIONS_MAX_CHARS = 8000;
export const TASK_BRIEF_MAX_CHARS = 12000;
export const TASK_ANSWER_MAX_CHARS = 8000;

/** `/app/api/tasks` (list + create). */
export function tasksEndpoint(): string {
  return `${API_BASE_URL}/tasks`;
}

/** `/app/api/tasks/{id}` (read, PATCH, delete; the base of every task route). */
export function taskEndpoint(id: string): string {
  return `${tasksEndpoint()}/${encodeURIComponent(id)}`;
}

// --- Tasks -----------------------------------------------------------------

/** The user's tasks, newest activity first (`archived: true` = the archived list). */
export function listTasks(opts: { archived?: boolean } = {}): Promise<ListTasksResponse> {
  return apiGet(tasksEndpoint(), { query: { archived: opts.archived ? 'true' : undefined } });
}

/**
 * Create a task (201). Rejects 400 `invalid_title` / `invalid_instructions`
 * / `invalid_model` / `invalid_thinking_effort` / `model_not_allowed`, 409
 * `tasks_cap_reached`.
 */
export function createTask(body: CreateTaskRequest): Promise<Task> {
  return apiPost(tasksEndpoint(), { body });
}

/** The task detail (with the current brief). */
export function getTask(id: string): Promise<Task> {
  return apiGet(taskEndpoint(id));
}

/**
 * Edit title, instructions, default model or thinking level. Only the keys
 * present are applied; null clears model / thinking_effort. Same 400 codes
 * as createTask.
 */
export function updateTask(id: string, body: UpdateTaskRequest): Promise<Task> {
  return apiPatch(taskEndpoint(id), { body });
}

/** Archive the task (409 `task_run_active` while a run is active); marks it read. */
export function archiveTask(id: string): Promise<Task> {
  return apiPut(`${taskEndpoint(id)}/archive`);
}

/** Unarchive the task (409 `tasks_cap_reached`). */
export function unarchiveTask(id: string): Promise<Task> {
  return apiPut(`${taskEndpoint(id)}/unarchive`);
}

/** Delete the task with its runs, their conversations and its files (stops an active run first). */
export function deleteTask(id: string): Promise<{ success: boolean }> {
  return apiDelete(taskEndpoint(id));
}

/**
 * Acknowledge the thread up to `seq` (sent only by a focused client showing
 * the thread). The server moves `read_seq` forward only, clamped to
 * `event_seq`, and returns the resulting seqs.
 */
export function markTaskRead(id: string, seq: number): Promise<TaskReadState> {
  return apiPost(`${taskEndpoint(id)}/read`, { body: { seq } });
}

// --- Brief -----------------------------------------------------------------

/**
 * Save the user's brief edit (allowed while a run is active). Rejects 409
 * `stale_brief` when `expectedBriefVersion` is not current (staleBriefCurrent
 * has the current text), 400 `brief_too_long` / `invalid_brief`.
 */
export function putTaskBrief(
  id: string,
  brief: string,
  expectedBriefVersion: number,
): Promise<TaskBriefState> {
  return apiPut(`${taskEndpoint(id)}/brief`, {
    body: { brief, expected_brief_version: expectedBriefVersion },
  });
}

/** The stored brief versions, newest first (version 0, the empty brief, has no row). */
export function listTaskBriefVersions(id: string): Promise<{ versions: TaskBriefVersion[] }> {
  return apiGet(`${taskEndpoint(id)}/brief/versions`);
}

/** One stored version with its full text. 404 `brief_version_not_found`. */
export function getTaskBriefVersion(id: string, version: number): Promise<TaskBriefVersionDetail> {
  return apiGet(`${taskEndpoint(id)}/brief/versions/${encodeURIComponent(String(version))}`);
}

/**
 * Write a stored version's text as a new brief version (source
 * `restore:<version>`). Rejects 409 `stale_brief`, 404
 * `brief_version_not_found`, 400 `brief_too_long`.
 */
export function restoreTaskBriefVersion(
  id: string,
  version: number,
  expectedBriefVersion: number,
): Promise<TaskBriefState> {
  return apiPost(
    `${taskEndpoint(id)}/brief/versions/${encodeURIComponent(String(version))}/restore`,
    { body: { expected_brief_version: expectedBriefVersion } },
  );
}

// --- Runs and questions ----------------------------------------------------

/**
 * One page of the thread, newest first. `beforeSeq` is the previous page's
 * `next_before_seq`; `limit` 1..100 (server default 20).
 */
export function listTaskRuns(
  id: string,
  opts: { beforeSeq?: number | null; limit?: number } = {},
): Promise<TaskRunsPage> {
  return apiGet(`${taskEndpoint(id)}/runs`, {
    query: { before_seq: opts.beforeSeq ?? undefined, limit: opts.limit },
  });
}

/**
 * Create the next run's conversation (201); the run starts with the first
 * `send_message` on it. Rejects 409 `task_run_active` (taskActiveRunFromError)
 * and 409 `task_archived`.
 */
export function createTaskRunConversation(id: string): Promise<{ conversation_id: string }> {
  return apiPost(`${taskEndpoint(id)}/runs`);
}

/** Delete a finished run's entry and its conversation. 404 `task_run_not_found`, 409 `task_run_active`. */
export function deleteTaskRun(id: string, runId: string): Promise<{ success: boolean }> {
  return apiDelete(`${taskEndpoint(id)}/runs/${encodeURIComponent(runId)}`);
}

/**
 * Answer a run's open question and continue the run. Rejects 404
 * `question_not_found`, 409 `question_resolved` (with `status`, `answer`),
 * 400 `invalid_answer`.
 */
export function answerTaskQuestion(
  id: string,
  handleId: string,
  answer: string,
): Promise<TaskQuestion> {
  return apiPost(`${taskEndpoint(id)}/questions/${encodeURIComponent(handleId)}/answer`, {
    body: { answer },
  });
}

// --- Error accessors -------------------------------------------------------

/**
 * The structured `detail` object of a task error (`{error, message,
 * ...extras}`), or null (not an ApiClientError, or a string body).
 */
export function taskErrorDetail(err: unknown): Record<string, unknown> | null {
  if (!(err instanceof ApiClientError)) return null;
  const detail = err.details?.detail;
  return detail && typeof detail === 'object' ? (detail as Record<string, unknown>) : null;
}

/** True for the 409 stale_brief of a brief save or restore. */
export function isStaleBriefError(err: unknown): err is ApiClientError {
  return err instanceof ApiClientError && err.errorCode === 'stale_brief';
}

/** The brief as the server has it now, from a stale_brief error, else null. */
export function staleBriefCurrent(
  err: unknown,
): { brief: string; brief_version: number; brief_updated_at: string | null } | null {
  if (!isStaleBriefError(err)) return null;
  const current = taskErrorDetail(err)?.current;
  if (!current || typeof current !== 'object') return null;
  const { brief, brief_version: version, brief_updated_at: updatedAt } =
    current as Record<string, unknown>;
  if (typeof brief !== 'string' || typeof version !== 'number') return null;
  return {
    brief,
    brief_version: version,
    brief_updated_at: typeof updatedAt === 'string' ? updatedAt : null,
  };
}

/**
 * Validate an `active_run` payload (`{id, seq, status}`) from an error body
 * or a realtime envelope; null when absent or malformed.
 */
export function parseTaskActiveRun(value: unknown): TaskActiveRun | null {
  if (!value || typeof value !== 'object') return null;
  const { id, seq, status } = value as Record<string, unknown>;
  if (typeof id !== 'string' || typeof seq !== 'number') return null;
  if (status !== 'running' && status !== 'waiting') return null;
  return { id, seq, status };
}

/** The task's active run from a 409 task_run_active error, else null. */
export function taskActiveRunFromError(err: unknown): TaskActiveRun | null {
  if (!(err instanceof ApiClientError) || err.errorCode !== 'task_run_active') return null;
  return parseTaskActiveRun(taskErrorDetail(err)?.active_run);
}
