/**
 * AutoProjects URL helpers. The URL is the source of truth for which AutoProjects
 * view is showing (no NavigationContext state), as for Quest Docs and Quest
 * Tables (utils/docsRoute.ts, utils/tablesRoute.ts):
 *
 *   /autoprojects                               AutoProjects list (unarchived)
 *   /autoprojects?archived=true                 Archived AutoProjects
 *   /autoprojects/new                           New AutoProject (creates the AutoProject on first send)
 *   /autoprojects/<id>                          AutoProject page
 *   /autoprojects/<id>/runs/<conversation id>   AutoProject page with that run's transcript
 *                                        open in its detail panel
 *
 * `new` is reserved and never an AutoProject id. Any other shape is not an AutoProjects
 * route. The server serves the SPA for all of these (quest.py
 * serve_spa_autoprojects). The run's conversation is deliberately not the
 * router's `conversationId` param (App.tsx): a run conversation never becomes
 * the active sidebar conversation.
 */

export type AutoProjectsRoute =
  | { kind: 'list'; archived: boolean }
  | { kind: 'new' }
  | { kind: 'page'; autoprojectId: string }
  | { kind: 'run'; autoprojectId: string; conversationId: string }

const AUTOPROJECTS_PREFIX = '/autoprojects'
const NEW_SEGMENT = 'new'
const RUNS_SEGMENT = 'runs'

/** Shape of an AutoProject or conversation id in the URL: uuid-like, nothing that could change a path. */
const ID_RE = /^[A-Za-z0-9_-]{1,64}$/

/**
 * Decode one path segment into an id, or null when it is not id-shaped.
 * Ids are uuid4 strings; anything else -- in particular a decoded "/" or
 * ".." -- must never be spliced into an /app/api/autoprojects/<id>/... URL.
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
 * Parse a location into an AutoProjects route, or null when the location is not a
 * AutoProjects URL. `search` is `location.search` (with or without the leading
 * "?"); only the list view reads it (`archived=true`, any other value =
 * the unarchived list).
 */
export function parseAutoProjectsRoute(pathname: string, search: string): AutoProjectsRoute | null {
  if (pathname === AUTOPROJECTS_PREFIX || pathname === `${AUTOPROJECTS_PREFIX}/`) {
    const archived = new URLSearchParams(search).get('archived') === 'true'
    return { kind: 'list', archived }
  }
  if (!pathname.startsWith(`${AUTOPROJECTS_PREFIX}/`)) return null

  let rest = pathname.slice(AUTOPROJECTS_PREFIX.length + 1)
  // One trailing slash is tolerated (/autoprojects/<id>/), matching the router.
  if (rest.endsWith('/')) rest = rest.slice(0, -1)
  if (!rest) return null
  const segments = rest.split('/')

  if (segments.length === 1) {
    if (segments[0] === NEW_SEGMENT) return { kind: 'new' }
    const autoprojectId = decodeId(segments[0])
    if (autoprojectId === null || autoprojectId === NEW_SEGMENT) return null
    return { kind: 'page', autoprojectId }
  }

  if (segments.length === 3 && segments[1] === RUNS_SEGMENT) {
    const autoprojectId = decodeId(segments[0])
    const conversationId = decodeId(segments[2])
    if (autoprojectId === null || autoprojectId === NEW_SEGMENT || conversationId === null) return null
    return { kind: 'run', autoprojectId, conversationId }
  }

  return null
}

/** URL of the AutoProjects list: the unarchived AutoProjects, or the archived ones. */
export function autoprojectsListPath(opts: { archived?: boolean } = {}): string {
  return opts.archived ? `${AUTOPROJECTS_PREFIX}?archived=true` : AUTOPROJECTS_PREFIX
}

/** URL of the New AutoProject page (an empty page that creates the AutoProject on first send). */
export function newAutoProjectPath(): string {
  return `${AUTOPROJECTS_PREFIX}/${NEW_SEGMENT}`
}

/** URL of one AutoProject's page. */
export function autoprojectPath(autoprojectId: string): string {
  return `${AUTOPROJECTS_PREFIX}/${encodeURIComponent(autoprojectId)}`
}

/** URL of an AutoProject's page with one run open in its detail panel, by the run's conversation id. */
export function autoprojectRunPath(autoprojectId: string, conversationId: string): string {
  return `${autoprojectPath(autoprojectId)}/${RUNS_SEGMENT}/${encodeURIComponent(conversationId)}`
}
