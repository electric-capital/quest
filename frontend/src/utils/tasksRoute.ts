/**
 * Quest Tasks URL helpers. The URL is the source of truth for which tasks
 * view is showing (no NavigationContext state), as for Quest Docs and Quest
 * Tables (utils/docsRoute.ts, utils/tablesRoute.ts):
 *
 *   /tasks                               Tasks list (unarchived)
 *   /tasks?archived=true                 Archived tasks
 *   /tasks/new                           New Task (an empty thread)
 *   /tasks/<id>                          Task thread
 *   /tasks/<id>/runs/<conversation id>   Run view (one run's transcript)
 *
 * `new` is reserved and never a task id. Any other shape is not a tasks
 * route. The server serves the SPA for all of these (quest.py
 * serve_spa_tasks). The run view's conversation is deliberately not the
 * router's `conversationId` param (App.tsx): a run conversation never becomes
 * the active sidebar conversation.
 */

export type TasksRoute =
  | { kind: 'list'; archived: boolean }
  | { kind: 'new' }
  | { kind: 'thread'; taskId: string }
  | { kind: 'run'; taskId: string; conversationId: string }

const TASKS_PREFIX = '/tasks'
const NEW_SEGMENT = 'new'
const RUNS_SEGMENT = 'runs'

/** Shape of a task or conversation id in the URL: uuid-like, nothing that could change a path. */
const ID_RE = /^[A-Za-z0-9_-]{1,64}$/

/**
 * Decode one path segment into an id, or null when it is not id-shaped.
 * Ids are uuid4 strings; anything else -- in particular a decoded "/" or
 * ".." -- must never be spliced into an /app/api/tasks/<id>/... URL.
 */
function decodeId(segment: string): string | null {
  let id: string
  try {
    id = decodeURIComponent(segment)
  } catch {
    return null
  }
  return ID_RE.test(id) ? id : null
}

/**
 * Parse a location into a tasks route, or null when the location is not a
 * tasks URL. `search` is `location.search` (with or without the leading
 * "?"); only the list view reads it (`archived=true`, any other value =
 * the unarchived list).
 */
export function parseTasksRoute(pathname: string, search: string): TasksRoute | null {
  if (pathname === TASKS_PREFIX || pathname === `${TASKS_PREFIX}/`) {
    const archived = new URLSearchParams(search).get('archived') === 'true'
    return { kind: 'list', archived }
  }
  if (!pathname.startsWith(`${TASKS_PREFIX}/`)) return null

  let rest = pathname.slice(TASKS_PREFIX.length + 1)
  // One trailing slash is tolerated (/tasks/<id>/), matching the router.
  if (rest.endsWith('/')) rest = rest.slice(0, -1)
  if (!rest) return null
  const segments = rest.split('/')

  if (segments.length === 1) {
    if (segments[0] === NEW_SEGMENT) return { kind: 'new' }
    const taskId = decodeId(segments[0])
    if (taskId === null || taskId === NEW_SEGMENT) return null
    return { kind: 'thread', taskId }
  }

  if (segments.length === 3 && segments[1] === RUNS_SEGMENT) {
    const taskId = decodeId(segments[0])
    const conversationId = decodeId(segments[2])
    if (taskId === null || taskId === NEW_SEGMENT || conversationId === null) return null
    return { kind: 'run', taskId, conversationId }
  }

  return null
}

/** URL of the tasks list: the unarchived tasks, or the archived ones. */
export function tasksListPath(opts: { archived?: boolean } = {}): string {
  return opts.archived ? `${TASKS_PREFIX}?archived=true` : TASKS_PREFIX
}

/** URL of the New Task page (an empty thread that creates the task on first send). */
export function newTaskPath(): string {
  return `${TASKS_PREFIX}/${NEW_SEGMENT}`
}

/** URL of one task's thread. */
export function taskThreadPath(taskId: string): string {
  return `${TASKS_PREFIX}/${encodeURIComponent(taskId)}`
}

/** URL of the run view for one run of a task, by the run's conversation id. */
export function taskRunPath(taskId: string, conversationId: string): string {
  return `${taskThreadPath(taskId)}/${RUNS_SEGMENT}/${encodeURIComponent(conversationId)}`
}
