import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  deleteTable,
  filterErrorPath,
  isStaleSchemaError,
  isStaleUpdateError,
  listTableChanges,
  listTables,
  queryTableRows,
  removeTableShare,
  restoreTable,
  rowsViolation,
  staleSchemaCurrent,
  staleUpdateCurrent,
  tableDownloadUrl,
  tableErrorDetail,
  updateTableSchema,
} from './tablesApi';
import { ApiClientError, handleErrorResponse } from './request';

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

async function errorFrom(status: number, body: unknown): Promise<unknown> {
  try {
    await handleErrorResponse(jsonResponse(status, body));
  } catch (err) {
    return err;
  }
  throw new Error('handleErrorResponse did not throw');
}

function stubFetch(response: () => Response) {
  const fetchMock = vi.fn(async () => response());
  vi.stubGlobal('fetch', fetchMock);
  return {
    calls: () => fetchMock.mock.calls as unknown as [string, RequestInit][],
  };
}

describe('tablesApi', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('maps list options onto the query string', async () => {
    const fetchMock = stubFetch(() => jsonResponse(200, { tables: [], has_more: false, next_cursor: null }));
    await listTables();
    await listTables({ projectId: 'p1', limit: 5, cursor: '2026-10-01T00:00:00|t9' });
    await listTables({ shared: true });
    const calls = fetchMock.calls();
    expect(calls[0][0]).toBe('/app/api/tables');
    expect(calls[1][0]).toBe(
      '/app/api/tables?project_id=p1&limit=5&cursor=2026-10-01T00%3A00%3A00%7Ct9',
    );
    expect(calls[2][0]).toBe('/app/api/tables?shared=true');
  });

  it('JSON-encodes filter and sort into the rows query', async () => {
    const fetchMock = stubFetch(() =>
      jsonResponse(200, { rows: [], total: 0, limit: 200, offset: 0, truncated: false, schema_version: 1 }),
    );
    const filter = {
      and: [
        { column: 'Status', op: 'eq' as const, value: 'done & dusted' },
        { not: { column: 'Price', op: 'between' as const, value: [1, 10] } },
      ],
    };
    const sort = [{ column: 'Price', direction: 'desc' as const }];
    await queryTableRows('t1', { filter, sort, limit: 200, offset: 400 });
    await queryTableRows('t1');
    await queryTableRows('t1', { countOnly: true, sort: [] });

    const [first, second, third] = fetchMock.calls().map(([url]) => url);
    const url = new URL(first, 'http://x');
    expect(url.pathname).toBe('/app/api/tables/t1/rows');
    expect(JSON.parse(url.searchParams.get('filter')!)).toEqual(filter);
    expect(JSON.parse(url.searchParams.get('sort')!)).toEqual(sort);
    expect(url.searchParams.get('limit')).toBe('200');
    expect(url.searchParams.get('offset')).toBe('400');
    expect(url.searchParams.has('count_only')).toBe(false);
    // No raw JSON punctuation leaks into the URL; the value's "&" cannot
    // split the parameter.
    expect(first).not.toMatch(/[ {}"[\]]/);
    expect(first.split('?')[1].split('&')).toHaveLength(4);
    expect(second).toBe('/app/api/tables/t1/rows');
    // An empty sort list is left out.
    expect(third).toBe('/app/api/tables/t1/rows?count_only=true');
  });

  it('sends the schema, restore and history requests', async () => {
    const fetchMock = stubFetch(() => jsonResponse(200, {}));
    await updateTableSchema('t1', {
      operations: [{ op: 'rename_column', name: 'A', new_name: 'B' }],
      expected_schema_version: 3,
    });
    await restoreTable('t1', 7);
    await listTableChanges('t1', { beforeId: 40, limit: 20 });
    await listTableChanges('t1');
    const calls = fetchMock.calls();
    expect(calls[0][0]).toBe('/app/api/tables/t1/schema');
    expect(calls[0][1].method).toBe('PUT');
    expect(JSON.parse(calls[0][1].body as string)).toEqual({
      operations: [{ op: 'rename_column', name: 'A', new_name: 'B' }],
      expected_schema_version: 3,
    });
    expect(calls[1][0]).toBe('/app/api/tables/t1/restore');
    expect(JSON.parse(calls[1][1].body as string)).toEqual({ change_id: 7 });
    expect(calls[2][0]).toBe('/app/api/tables/t1/changes?limit=20&before_id=40');
    expect(calls[3][0]).toBe('/app/api/tables/t1/changes');
  });

  it('accepts the empty 204 of a delete and maps a delete error', async () => {
    let fetchMock = stubFetch(() => new Response(null, { status: 204 }));
    await expect(deleteTable('t1')).resolves.toBeUndefined();
    expect(fetchMock.calls()[0][0]).toBe('/app/api/tables/t1');
    expect(fetchMock.calls()[0][1].method).toBe('DELETE');

    fetchMock = stubFetch(() =>
      jsonResponse(403, { detail: { error: 'forbidden', message: 'Only the owner' } }),
    );
    const err = await deleteTable('t1').catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiClientError);
    expect((err as ApiClientError).errorCode).toBe('forbidden');
    expect((err as ApiClientError).statusCode).toBe(403);
  });

  it('removes a share and returns the owner row', async () => {
    const fetchMock = stubFetch(() => jsonResponse(200, { id: 't1', shares: [] }));
    await expect(removeTableShare('t1', 5)).resolves.toEqual({ id: 't1', shares: [] });
    expect(fetchMock.calls()[0][0]).toBe('/app/api/tables/t1/shares/5');
  });

  it('builds the cookie-authed download URLs', () => {
    expect(tableDownloadUrl('t1')).toBe('/app/api/tables/t1/download?format=csv');
    expect(tableDownloadUrl('t1', 'csv')).toBe('/app/api/tables/t1/download?format=csv');
    expect(tableDownloadUrl('t1', 'sqlite')).toBe('/app/api/tables/t1/download?format=sqlite');
    expect(tableDownloadUrl('t1', 'sqlite', true)).toBe(
      '/app/api/tables/t1/download?format=sqlite&include_history=true',
    );
    expect(tableDownloadUrl('a/b')).toBe('/app/api/tables/a%2Fb/download?format=csv');
  });

  it('recognizes the flat stale_update 409 and returns its current row', async () => {
    const current = { id: 't1', title: 'Theirs', updated_at: '2026-10-02T00:00:00' };
    const err = await errorFrom(409, { error: 'stale_update', message: 'Changed', current });
    expect(isStaleUpdateError(err)).toBe(true);
    expect(staleUpdateCurrent(err)).toEqual(current);
    // Flat body: no structured detail.
    expect(tableErrorDetail(err)).toBeNull();
  });

  it('does not treat other errors as stale updates', async () => {
    const duplicate = await errorFrom(409, { detail: { error: 'duplicate_title', message: 'Taken' } });
    expect((duplicate as ApiClientError).errorCode).toBe('duplicate_title');
    expect(isStaleUpdateError(duplicate)).toBe(false);
    expect(staleUpdateCurrent(duplicate)).toBeNull();
    expect(isStaleUpdateError(new Error('stale_update'))).toBe(false);
    expect(staleUpdateCurrent(new ApiClientError('x', 409, 'stale_update'))).toBeNull();
  });

  it('pulls the current schema out of a stale_schema 409', async () => {
    const schema = [{ key: 'c_0000000a', name: 'A', type: 'text' }];
    const err = await errorFrom(409, {
      detail: { error: 'stale_schema', message: 'Moved', schema_version: 4, schema },
    });
    expect(isStaleSchemaError(err)).toBe(true);
    expect(staleSchemaCurrent(err)).toEqual({ schema_version: 4, schema });
    expect(staleSchemaCurrent(new ApiClientError('x', 409, 'stale_schema'))).toBeNull();
    expect(isStaleSchemaError(await errorFrom(409, { detail: { error: 'busy' } }))).toBe(false);
  });

  it('pulls the offending rows out of a rows_violate 400', async () => {
    const err = await errorFrom(400, {
      detail: {
        error: 'rows_violate',
        message: '3 rows have no value',
        column: 'Status',
        check: 'required',
        option: null,
        row_ids: [4, 9, 12],
        total: 3,
      },
    });
    expect(rowsViolation(err)).toEqual({
      column: 'Status', check: 'required', option: null, row_ids: [4, 9, 12], total: 3,
    });
    expect(rowsViolation(await errorFrom(400, { detail: { error: 'invalid_args' } }))).toBeNull();
  });

  it('reads the path of a filter error, root included', async () => {
    const nested = await errorFrom(400, {
      detail: { error: 'op_not_allowed', message: 'No', path: 'and[1].op' },
    });
    expect((nested as ApiClientError).errorCode).toBe('op_not_allowed');
    expect(filterErrorPath(nested)).toBe('and[1].op');
    const root = await errorFrom(400, { detail: { error: 'invalid_filter', message: 'Bad', path: '' } });
    expect(filterErrorPath(root)).toBe('');
    expect(filterErrorPath(await errorFrom(404, { detail: { error: 'table_not_found' } }))).toBeNull();
  });
});
