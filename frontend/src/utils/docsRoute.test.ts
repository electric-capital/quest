import { describe, expect, it } from 'vitest'
import { docViewerPath, docsListPath, parseDocsRoute } from './docsRoute'

describe('parseDocsRoute', () => {
  it('parses the unfiltered list view', () => {
    expect(parseDocsRoute('/docs', '')).toEqual({ kind: 'list', projectId: null })
  })

  it('treats /docs/ like /docs', () => {
    expect(parseDocsRoute('/docs/', '')).toEqual({ kind: 'list', projectId: null })
    expect(parseDocsRoute('/docs/', '?project=abc')).toEqual({ kind: 'list', projectId: 'abc' })
  })

  it('reads the project filter from the query string', () => {
    expect(parseDocsRoute('/docs', '?project=abc')).toEqual({ kind: 'list', projectId: 'abc' })
    // Leading "?" is optional.
    expect(parseDocsRoute('/docs', 'project=abc')).toEqual({ kind: 'list', projectId: 'abc' })
  })

  it('treats an empty project= as no filter', () => {
    expect(parseDocsRoute('/docs', '?project=')).toEqual({ kind: 'list', projectId: null })
  })

  it('ignores unrelated query params', () => {
    expect(parseDocsRoute('/docs', '?foo=1')).toEqual({ kind: 'list', projectId: null })
  })

  it('parses the viewer route', () => {
    expect(parseDocsRoute('/docs/xyz', '')).toEqual({ kind: 'viewer', docId: 'xyz' })
    // The query string does not matter for the viewer.
    expect(parseDocsRoute('/docs/xyz', '?project=abc')).toEqual({ kind: 'viewer', docId: 'xyz' })
  })

  it('tolerates one trailing slash on the viewer route', () => {
    expect(parseDocsRoute('/docs/xyz/', '')).toEqual({ kind: 'viewer', docId: 'xyz' })
  })

  it('accepts uuid-like ids only', () => {
    expect(parseDocsRoute('/docs/3f2a9c1e-7b4d-4e8a-9c21-0f6d8e5a1b2c', '')).toEqual({
      kind: 'viewer',
      docId: '3f2a9c1e-7b4d-4e8a-9c21-0f6d8e5a1b2c',
    })
    // A decoded "/" or ".." is not a doc id (it would be spliced into
    // /app/api/docs/<id>/... URLs otherwise), nor is whitespace.
    expect(parseDocsRoute('/docs/x%2Fdownload', '')).toBeNull()
    expect(parseDocsRoute('/docs/..%2Fy', '')).toBeNull()
    expect(parseDocsRoute('/docs/a%20b', '')).toBeNull()
    expect(parseDocsRoute('/docs/%E2%9C%93', '')).toBeNull()
  })

  it('rejects deeper paths', () => {
    expect(parseDocsRoute('/docs/xyz/more', '')).toBeNull()
    expect(parseDocsRoute('/docs//', '')).toBeNull()
  })

  it('rejects non-docs paths', () => {
    expect(parseDocsRoute('/docsx', '')).toBeNull()
    expect(parseDocsRoute('/', '')).toBeNull()
    expect(parseDocsRoute('/chats/abc', '')).toBeNull()
    expect(parseDocsRoute('/api-docs', '')).toBeNull()
    expect(parseDocsRoute('/app/api/docs', '')).toBeNull()
  })

  it('rejects a malformed percent-encoding', () => {
    expect(parseDocsRoute('/docs/%E0%A4%A', '')).toBeNull()
  })
})

describe('docsListPath', () => {
  it('returns /docs without a project', () => {
    expect(docsListPath()).toBe('/docs')
    expect(docsListPath(null)).toBe('/docs')
    expect(docsListPath('')).toBe('/docs')
  })

  it('adds an encoded project filter', () => {
    expect(docsListPath('abc')).toBe('/docs?project=abc')
    expect(docsListPath('a&b c')).toBe('/docs?project=a%26b%20c')
  })

  it('round-trips through parseDocsRoute', () => {
    const path = docsListPath('a&b c')
    const [pathname, query] = path.split('?')
    expect(parseDocsRoute(pathname, `?${query}`)).toEqual({ kind: 'list', projectId: 'a&b c' })
  })
})

describe('docViewerPath', () => {
  it('returns /docs/<encoded id>', () => {
    expect(docViewerPath('xyz')).toBe('/docs/xyz')
    expect(docViewerPath('a/b')).toBe('/docs/a%2Fb')
  })

  it('round-trips through parseDocsRoute', () => {
    expect(parseDocsRoute(docViewerPath('doc_1-A'), '')).toEqual({ kind: 'viewer', docId: 'doc_1-A' })
  })
})
