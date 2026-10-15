/**
 * Quest Tables URL helpers. The URL is the source of truth for which tables
 * view is showing (no NavigationContext state), as for Quest Docs
 * (utils/docsRoute.ts):
 *
 *   /tables                         All Tables (the user's tables)
 *   /tables?project=<id>            All Tables filtered to one project
 *   /tables/<id>                    Table viewer
 *   /tables/<id>?filter=..&sort=..  Viewer with a linkable filter / sort
 *
 * `filter` and `sort` are the raw JSON strings of the query_rows DSL (spec
 * 6.1), carried through unparsed; the filter bar decodes and validates them.
 * Anything deeper than /tables/<id> is not a tables route. The server serves
 * the SPA for all of these (quest.py serve_spa_tables).
 */

export type TablesRoute =
  | { kind: 'list'; projectId: string | null }
  | { kind: 'viewer'; tableId: string; filter: string | null; sort: string | null }

const TABLES_PREFIX = '/tables'

/**
 * Parse a location into a tables route, or null when the location is not a
 * tables URL. `search` is `location.search` (with or without the leading
 * "?"); the list view reads `project`, the viewer `filter` and `sort` (an
 * empty value = absent).
 */
export function parseTablesRoute(pathname: string, search: string): TablesRoute | null {
  const params = new URLSearchParams(search)
  if (pathname === TABLES_PREFIX || pathname === `${TABLES_PREFIX}/`) {
    const projectId = params.get('project')
    return { kind: 'list', projectId: projectId ? projectId : null }
  }
  if (!pathname.startsWith(`${TABLES_PREFIX}/`)) return null

  let rest = pathname.slice(TABLES_PREFIX.length + 1)
  // One trailing slash is tolerated (/tables/<id>/), matching the router.
  if (rest.endsWith('/')) rest = rest.slice(0, -1)
  if (!rest || rest.includes('/')) return null

  let tableId: string
  try {
    tableId = decodeURIComponent(rest)
  } catch {
    return null
  }
  // Table ids are uuid4 strings. Anything else -- in particular a decoded
  // "/" or ".." -- is not a table id and must never be spliced into an
  // /app/api/tables/<id>/... URL.
  if (!TABLE_ID_RE.test(tableId)) return null
  return {
    kind: 'viewer',
    tableId,
    filter: params.get('filter') || null,
    sort: params.get('sort') || null,
  }
}

/** Shape of a table id in the URL: uuid-like, nothing that could change a path. */
const TABLE_ID_RE = /^[A-Za-z0-9_-]{1,64}$/

/** URL of the All Tables view, optionally filtered to one project. */
export function tablesListPath(projectId?: string | null): string {
  return projectId
    ? `${TABLES_PREFIX}?project=${encodeURIComponent(projectId)}`
    : TABLES_PREFIX
}

/**
 * URL of the viewer for one table, with an optional raw filter / sort JSON
 * string (empty / null = left out).
 */
export function tableViewerPath(
  tableId: string,
  opts: { filter?: string | null; sort?: string | null } = {},
): string {
  const query: string[] = []
  if (opts.filter) query.push(`filter=${encodeURIComponent(opts.filter)}`)
  if (opts.sort) query.push(`sort=${encodeURIComponent(opts.sort)}`)
  const base = `${TABLES_PREFIX}/${encodeURIComponent(tableId)}`
  return query.length > 0 ? `${base}?${query.join('&')}` : base
}
