/**
 * Quest Docs URL helpers. The URL is the source of truth for which docs view
 * is showing (no NavigationContext state):
 *
 *   /docs                 All Docs (the user's docs)
 *   /docs?project=<id>    All Docs filtered to one project
 *   /docs/<id>            Doc viewer
 *
 * Anything deeper than /docs/<id> is not a docs route. The server serves the
 * SPA for all of these (quest.py serve_spa_docs); Swagger UI lives at
 * /api-docs.
 */

export type DocsRoute =
  | { kind: 'list'; projectId: string | null }
  | { kind: 'viewer'; docId: string }

const DOCS_PREFIX = '/docs'

/**
 * Parse a location into a docs route, or null when the location is not a
 * docs URL. `search` is `location.search` (with or without the leading "?");
 * only the list view reads it (`project`, empty value = no filter).
 */
export function parseDocsRoute(pathname: string, search: string): DocsRoute | null {
  if (pathname === DOCS_PREFIX || pathname === `${DOCS_PREFIX}/`) {
    const projectId = new URLSearchParams(search).get('project')
    return { kind: 'list', projectId: projectId ? projectId : null }
  }
  if (!pathname.startsWith(`${DOCS_PREFIX}/`)) return null

  let rest = pathname.slice(DOCS_PREFIX.length + 1)
  // One trailing slash is tolerated (/docs/<id>/), matching the router.
  if (rest.endsWith('/')) rest = rest.slice(0, -1)
  if (!rest || rest.includes('/')) return null

  let docId: string
  try {
    docId = decodeURIComponent(rest)
  } catch {
    return null
  }
  return docId ? { kind: 'viewer', docId } : null
}

/** URL of the All Docs view, optionally filtered to one project. */
export function docsListPath(projectId?: string | null): string {
  return projectId
    ? `${DOCS_PREFIX}?project=${encodeURIComponent(projectId)}`
    : DOCS_PREFIX
}

/** URL of the viewer for one doc. */
export function docViewerPath(docId: string): string {
  return `${DOCS_PREFIX}/${encodeURIComponent(docId)}`
}
