/**
 * API client for AutoProjects (/app/api/autoprojects; see the module docstrings of
 * chat/autoprojects/routes.py, chat/autoprojects/views.py for the response shapes and
 * chat/autoprojects/http.py for the error bodies).
 *
 * Every call goes through the shared request wrapper, so failures reject
 * with an ApiClientError carrying the server's error code (`errorCode`) and
 * the whole parsed body (`details`). Every AutoProject route checks the gate first
 * (403 `autoprojects_disabled`), then ownership (404 `autoproject_not_found`, the same
 * body for a missing AutoProject and someone else's). AutoProject errors are FastAPI
 * envelopes, `{detail: {error, message, ...extras}}`; the extras (the
 * `current` brief of a 409 stale_brief, the `active_run` of a 409
 * autoproject_run_active, the `cap` / `actual` of a 400 brief_too_long) are pulled
 * out by the accessors at the bottom.
 *
 * A run is not started here: POST /autoprojects/{id}/runs only creates the run's
 * conversation, and the run starts with the first `send_message` on it
 * (services/autoprojectRuns.ts does both).
 */

import { API_BASE_URL } from './config';
import { ApiClientError, apiDelete, apiErrorDetail, apiGet, apiPatch, apiPost, apiPut } from './request';
import type {
  CreateAutoProjectRequest,
  ListAutoProjectsResponse,
  AutoProject,
  AutoProjectActiveRun,
  AutoProjectBriefState,
  AutoProjectBriefVersion,
  AutoProjectBriefVersionDetail,
  AutoProjectQuestion,
  AutoProjectReadState,
  AutoProjectRunsPage,
  UpdateAutoProjectRequest,
} from './types';

/** Feature-gate name; the UI shows AutoProjects only when `enabledFeatures` has it. */
export const AUTOPROJECTS_FEATURE = 'autoprojects';

/** Server default page size of GET /autoprojects/{id}/runs. */
export const AUTOPROJECT_RUNS_PAGE_SIZE = 20;

/** Server cap on a runs page (`limit` must be 1..100). */
export const AUTOPROJECT_RUNS_MAX_PAGE_SIZE = 100;

// Hand-mirrored caps of chat/autoprojects/constants.py, for the editors' counters.
// The server enforces them; keep these in step when one changes there.
export const AUTOPROJECT_TITLE_MAX_CHARS = 120;
export const AUTOPROJECT_BRIEF_MAX_CHARS = 12000;

/** `/app/api/autoprojects` (list + create). */
export function autoprojectsEndpoint(): string {
  return `${API_BASE_URL}/autoprojects`;
}

/** `/app/api/autoprojects/{id}` (read, PATCH, delete; the base of every AutoProject route). */
export function autoprojectEndpoint(id: string): string {
  return `${autoprojectsEndpoint()}/${encodeURIComponent(id)}`;
}

// --- AutoProjects -----------------------------------------------------------------

/** The user's AutoProjects, newest activity first (`archived: true` = the archived list). */
export function listAutoProjects(opts: { archived?: boolean } = {}): Promise<ListAutoProjectsResponse> {
  return apiGet(autoprojectsEndpoint(), { query: { archived: opts.archived ? 'true' : undefined } });
}

/**
 * Create an AutoProject (201). Rejects 400 `invalid_title` / `invalid_model` /
 * `invalid_thinking_effort` / `model_not_allowed`, 409 `autoprojects_cap_reached`.
 */
export function createAutoProject(body: CreateAutoProjectRequest): Promise<AutoProject> {
  return apiPost(autoprojectsEndpoint(), { body });
}

/** The AutoProject detail (with the current brief). */
export function getAutoProject(id: string): Promise<AutoProject> {
  return apiGet(autoprojectEndpoint(id));
}

/**
 * Edit the title, default model or thinking level. Only the keys
 * present are applied; null clears model / thinking_effort. Same 400 codes
 * as createAutoProject.
 */
export function updateAutoProject(id: string, body: UpdateAutoProjectRequest): Promise<AutoProject> {
  return apiPatch(autoprojectEndpoint(id), { body });
}

/** Archive the AutoProject (409 `autoproject_run_active` while a run is active); marks it read. */
export function archiveAutoProject(id: string): Promise<AutoProject> {
  return apiPut(`${autoprojectEndpoint(id)}/archive`);
}

/** Unarchive the AutoProject (409 `autoprojects_cap_reached`). */
export function unarchiveAutoProject(id: string): Promise<AutoProject> {
  return apiPut(`${autoprojectEndpoint(id)}/unarchive`);
}

// Ids of AutoProjects this tab is deleting or has deleted. The server publishes a
// last `autoproject_changed` for the AutoProject before the DELETE returns; the AutoProject hooks
// skip re-fetching such an id (isAutoProjectDeleting), which would only 404 while
// the page is still mounted. An id leaves the set again when its delete fails.
const deletingAutoProjectIds = new Set<string>();

/** True while (and after) this tab deletes the AutoProject: its hooks skip re-fetches. */
export function isAutoProjectDeleting(id: string): boolean {
  return deletingAutoProjectIds.has(id);
}

/**
 * Delete the AutoProject with its runs, their conversations and its files
 * (stops an active run first).
 */
export async function deleteAutoProject(id: string): Promise<{ success: boolean }> {
  deletingAutoProjectIds.add(id);
  try {
    return await apiDelete(autoprojectEndpoint(id));
  } catch (err) {
    deletingAutoProjectIds.delete(id);
    throw err;
  }
}

/**
 * Acknowledge the AutoProject's updates up to `seq` (sent only by a focused
 * client showing its page). The server moves `read_seq` forward only, clamped to
 * `event_seq`, and returns the resulting seqs.
 */
export function markAutoProjectRead(id: string, seq: number): Promise<AutoProjectReadState> {
  return apiPost(`${autoprojectEndpoint(id)}/read`, { body: { seq } });
}

// --- Brief -----------------------------------------------------------------

/**
 * Save the user's brief edit (allowed while a run is active). Rejects 409
 * `stale_brief` when `expectedBriefVersion` is not current (staleBriefCurrent
 * has the current text), 400 `brief_too_long` / `invalid_brief`.
 */
export function putAutoProjectBrief(
  id: string,
  brief: string,
  expectedBriefVersion: number,
): Promise<AutoProjectBriefState> {
  return apiPut(`${autoprojectEndpoint(id)}/brief`, {
    body: { brief, expected_brief_version: expectedBriefVersion },
  });
}

/** The stored brief versions, newest first (version 0, the empty brief, has no row). */
export function listAutoProjectBriefVersions(id: string): Promise<{ versions: AutoProjectBriefVersion[] }> {
  return apiGet(`${autoprojectEndpoint(id)}/brief/versions`);
}

/** One stored version with its full text. 404 `brief_version_not_found`. */
export function getAutoProjectBriefVersion(id: string, version: number): Promise<AutoProjectBriefVersionDetail> {
  return apiGet(`${autoprojectEndpoint(id)}/brief/versions/${encodeURIComponent(String(version))}`);
}

/**
 * Write a stored version's text as a new brief version (source
 * `restore:<version>`). Rejects 409 `stale_brief`, 404
 * `brief_version_not_found`, 400 `brief_too_long`.
 */
export function restoreAutoProjectBriefVersion(
  id: string,
  version: number,
  expectedBriefVersion: number,
): Promise<AutoProjectBriefState> {
  return apiPost(
    `${autoprojectEndpoint(id)}/brief/versions/${encodeURIComponent(String(version))}/restore`,
    { body: { expected_brief_version: expectedBriefVersion } },
  );
}

// --- Runs and questions ----------------------------------------------------

/**
 * One page of the AutoProject's runs (its updates), newest first. `beforeSeq`
 * is the previous page's `next_before_seq`; `limit` 1..100 (server default 20).
 */
export function listAutoProjectRuns(
  id: string,
  opts: { beforeSeq?: number | null; limit?: number } = {},
): Promise<AutoProjectRunsPage> {
  return apiGet(`${autoprojectEndpoint(id)}/runs`, {
    query: { before_seq: opts.beforeSeq ?? undefined, limit: opts.limit },
  });
}

/**
 * Create the next run's conversation (201); the run starts with the first
 * `send_message` on it. Rejects 409 `autoproject_run_active` (autoprojectActiveRunFromError)
 * and 409 `autoproject_archived`.
 */
export function createAutoProjectRunConversation(id: string): Promise<{ conversation_id: string }> {
  return apiPost(`${autoprojectEndpoint(id)}/runs`);
}

/**
 * Delete a finished run's entry and its conversation. 404
 * `autoproject_run_not_found`, 409 `autoproject_run_active`.
 */
export function deleteAutoProjectRun(id: string, runId: string): Promise<{ success: boolean }> {
  return apiDelete(`${autoprojectEndpoint(id)}/runs/${encodeURIComponent(runId)}`);
}

/**
 * Answer a run's open question and continue the run. Rejects 404
 * `question_not_found`, 409 `question_resolved` (with `status`, `answer`),
 * 400 `invalid_answer`.
 */
export function answerAutoProjectQuestion(
  id: string,
  handleId: string,
  answer: string,
): Promise<AutoProjectQuestion> {
  return apiPost(`${autoprojectEndpoint(id)}/questions/${encodeURIComponent(handleId)}/answer`, {
    body: { answer },
  });
}

// --- Error accessors -------------------------------------------------------

/** True for the 409 stale_brief of a brief save or restore. */
export function isStaleBriefError(err: unknown): err is ApiClientError {
  return err instanceof ApiClientError && err.errorCode === 'stale_brief';
}

/** The brief as the server has it now, from a stale_brief error, else null. */
export function staleBriefCurrent(
  err: unknown,
): { brief: string; brief_version: number; brief_updated_at: string | null } | null {
  if (!isStaleBriefError(err)) return null;
  const current = apiErrorDetail(err)?.current;
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
export function parseAutoProjectActiveRun(value: unknown): AutoProjectActiveRun | null {
  if (!value || typeof value !== 'object') return null;
  const { id, seq, status } = value as Record<string, unknown>;
  if (typeof id !== 'string' || typeof seq !== 'number') return null;
  if (status !== 'running' && status !== 'waiting') return null;
  return { id, seq, status };
}

/** The AutoProject's active run from a 409 autoproject_run_active error, else null. */
export function autoprojectActiveRunFromError(err: unknown): AutoProjectActiveRun | null {
  if (!(err instanceof ApiClientError) || err.errorCode !== 'autoproject_run_active') return null;
  return parseAutoProjectActiveRun(apiErrorDetail(err)?.active_run);
}
