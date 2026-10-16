import { describe, expect, it } from 'vitest'
import type { TableDetail } from '../api/types'
import {
  parseTablesRoute,
  tableFromLocationState,
  tableViewerPath,
  tableViewerSearch,
  tablesListPath,
} from './tablesRoute'

const UUID = '3f2a9c1e-7b4d-4e8a-9c21-0f6d8e5a1b2c'

function viewer(tableId: string, filter: string | null = null, sort: string | null = null) {
  return { kind: 'viewer', tableId, filter, sort }
}

/** Split a built path and parse it back. */
function roundTrip(path: string) {
  const index = path.indexOf('?')
  return index < 0
    ? parseTablesRoute(path, '')
    : parseTablesRoute(path.slice(0, index), path.slice(index))
}

describe('parseTablesRoute', () => {
  it('parses the unfiltered list view', () => {
    expect(parseTablesRoute('/tables', '')).toEqual({ kind: 'list', projectId: null })
  })

  it('treats /tables/ like /tables', () => {
    expect(parseTablesRoute('/tables/', '')).toEqual({ kind: 'list', projectId: null })
    expect(parseTablesRoute('/tables/', '?project=abc')).toEqual({ kind: 'list', projectId: 'abc' })
  })

  it('reads the project filter from the query string', () => {
    expect(parseTablesRoute('/tables', '?project=abc')).toEqual({ kind: 'list', projectId: 'abc' })
    // Leading "?" is optional.
    expect(parseTablesRoute('/tables', 'project=abc')).toEqual({ kind: 'list', projectId: 'abc' })
  })

  it('treats an empty project= as no filter and ignores unrelated params', () => {
    expect(parseTablesRoute('/tables', '?project=')).toEqual({ kind: 'list', projectId: null })
    expect(parseTablesRoute('/tables', '?foo=1')).toEqual({ kind: 'list', projectId: null })
    // filter / sort belong to the viewer, not the list.
    expect(parseTablesRoute('/tables', '?filter=x')).toEqual({ kind: 'list', projectId: null })
  })

  it('parses the viewer route', () => {
    expect(parseTablesRoute(`/tables/${UUID}`, '')).toEqual(viewer(UUID))
    // project= does not matter for the viewer.
    expect(parseTablesRoute('/tables/xyz', '?project=abc')).toEqual(viewer('xyz'))
  })

  it('tolerates one trailing slash on the viewer route', () => {
    expect(parseTablesRoute('/tables/xyz/', '')).toEqual(viewer('xyz'))
  })

  it('carries filter and sort as raw strings', () => {
    const filter = '{"column":"Status","op":"eq","value":"done"}'
    const sort = '[{"column":"Price","direction":"desc"}]'
    const search = `?filter=${encodeURIComponent(filter)}&sort=${encodeURIComponent(sort)}`
    expect(parseTablesRoute('/tables/xyz', search)).toEqual(viewer('xyz', filter, sort))
    expect(parseTablesRoute('/tables/xyz', `?sort=${encodeURIComponent(sort)}`)).toEqual(
      viewer('xyz', null, sort),
    )
    // Not parsed as JSON here: garbage passes through for the filter bar to refuse.
    expect(parseTablesRoute('/tables/xyz', '?filter=not-json')).toEqual(viewer('xyz', 'not-json'))
    // Empty values are absent.
    expect(parseTablesRoute('/tables/xyz', '?filter=&sort=')).toEqual(viewer('xyz'))
  })

  it('accepts uuid-like ids only', () => {
    // A decoded "/" or ".." is not a table id (it would be spliced into
    // /app/api/tables/<id>/... URLs otherwise), nor is whitespace or non-ASCII.
    expect(parseTablesRoute('/tables/x%2Fdownload', '')).toBeNull()
    expect(parseTablesRoute('/tables/..%2Fy', '')).toBeNull()
    expect(parseTablesRoute('/tables/..', '')).toBeNull()
    expect(parseTablesRoute('/tables/a%20b', '')).toBeNull()
    expect(parseTablesRoute('/tables/%E2%9C%93', '')).toBeNull()
    expect(parseTablesRoute(`/tables/${'a'.repeat(65)}`, '')).toBeNull()
  })

  it('rejects deeper paths', () => {
    expect(parseTablesRoute('/tables/xyz/more', '')).toBeNull()
    expect(parseTablesRoute('/tables/xyz/rows/', '')).toBeNull()
    expect(parseTablesRoute('/tables//', '')).toBeNull()
  })

  it('rejects non-tables paths', () => {
    expect(parseTablesRoute('/tablesx', '')).toBeNull()
    expect(parseTablesRoute('/', '')).toBeNull()
    expect(parseTablesRoute('/docs/abc', '')).toBeNull()
    expect(parseTablesRoute('/app/api/tables', '')).toBeNull()
    expect(parseTablesRoute('/projects/p/tables', '')).toBeNull()
  })

  it('rejects a malformed percent-encoding', () => {
    expect(parseTablesRoute('/tables/%E0%A4%A', '')).toBeNull()
  })
})

describe('tablesListPath', () => {
  it('returns /tables without a project', () => {
    expect(tablesListPath()).toBe('/tables')
    expect(tablesListPath(null)).toBe('/tables')
    expect(tablesListPath('')).toBe('/tables')
  })

  it('adds an encoded project filter that round-trips', () => {
    expect(tablesListPath('abc')).toBe('/tables?project=abc')
    expect(tablesListPath('a&b c')).toBe('/tables?project=a%26b%20c')
    expect(roundTrip(tablesListPath('a&b c'))).toEqual({ kind: 'list', projectId: 'a&b c' })
  })
})

describe('tableViewerPath', () => {
  it('returns /tables/<encoded id>', () => {
    expect(tableViewerPath('xyz')).toBe('/tables/xyz')
    expect(tableViewerPath('a/b')).toBe('/tables/a%2Fb')
    expect(tableViewerPath('xyz', { filter: '', sort: null })).toBe('/tables/xyz')
  })

  it('encodes filter and sort JSON and round-trips them', () => {
    const filter = JSON.stringify({
      and: [
        { column: 'Name & Co', op: 'contains', value: 'a+b #1 50%' },
        { column: 'Price', op: 'between', value: [1, 10] },
      ],
    })
    const sort = JSON.stringify([{ column: 'Price', direction: 'asc' }])
    const path = tableViewerPath(UUID, { filter, sort })
    expect(path.startsWith(`/tables/${UUID}?filter=`)).toBe(true)
    // Nothing that could end the parameter or the URL survives unencoded.
    expect(path.slice(path.indexOf('?') + 1).split('&')).toHaveLength(2)
    expect(path).not.toMatch(/[ #+"{}[\]]/)
    expect(roundTrip(path)).toEqual(viewer(UUID, filter, sort))
    expect(roundTrip(tableViewerPath('t_1-A', { sort }))).toEqual(viewer('t_1-A', null, sort))
  })
})

describe('tableViewerSearch', () => {
  it('sets and removes filter / sort, keeping other params', () => {
    const filter = '{"column":"A & B","op":"contains","value":"x+y #2 100%"}'
    const sort = '[{"column":"A & B","direction":"asc"}]'
    const search = tableViewerSearch('?other=1', { filter, sort })
    expect(parseTablesRoute('/tables/xyz', search)).toEqual(viewer('xyz', filter, sort))
    expect(new URLSearchParams(search).get('other')).toBe('1')
    expect(search).not.toMatch(/[ #"{}]/)
    expect(tableViewerSearch(search, { filter: null, sort })).toBe(
      `?other=1&sort=${new URLSearchParams({ sort }).toString().slice(5)}`,
    )
    expect(tableViewerSearch(search, { filter: '', sort: null })).toBe('?other=1')
    expect(tableViewerSearch('?filter=x', { filter: null, sort: null })).toBe('')
  })
})

describe('tableFromLocationState', () => {
  const table = { id: UUID, title: 'Leads', schema: [] } as unknown as TableDetail

  it('returns the carried detail for its own id only', () => {
    expect(tableFromLocationState({ table }, UUID)).toBe(table)
    expect(tableFromLocationState({ table }, 'other')).toBeNull()
  })

  it('ignores missing, foreign or malformed state', () => {
    expect(tableFromLocationState(null, UUID)).toBeNull()
    expect(tableFromLocationState(undefined, UUID)).toBeNull()
    expect(tableFromLocationState('text', UUID)).toBeNull()
    expect(tableFromLocationState({ from: '/tables' }, UUID)).toBeNull()
    expect(tableFromLocationState({ table: { id: UUID } }, UUID)).toBeNull()
    expect(tableFromLocationState({ table: 'x' }, UUID)).toBeNull()
  })
})
