// useTableRows: params encoding, refetch on param and refreshKey changes,
// out-of-order responses dropped, refreshKey bursts coalesced, the previous
// page kept during a refetch, errors kept as ApiClientError.
import { act, renderHook, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiClientError } from '../api/request';
import type { TableRowsPage } from '../api/types';
import { filterErrorPath } from '../api/tablesApi';
import { useTableRows, type UseTableRowsOptions } from './useTableRows';

const mocks = vi.hoisted(() => ({
  apiGet: vi.fn<(url: string, opts?: { query?: Record<string, unknown> }) => Promise<unknown>>(),
}));

vi.mock('../api/request', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/request')>()),
  apiGet: mocks.apiGet,
}));

function page(total: number, offset = 0, marker = 'x'): TableRowsPage {
  return {
    rows: [{ _id: total, _updated_at: '2026-10-01T00:00:00', c_a: marker }],
    total,
    limit: 200,
    offset,
    truncated: false,
    schema_version: 1,
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (err: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function setup(initial: UseTableRowsOptions & { tableId?: string | null } = {}) {
  return renderHook(
    ({ tableId, ...opts }: UseTableRowsOptions & { tableId?: string | null }) =>
      useTableRows(tableId === undefined ? 't1' : tableId, opts),
    { initialProps: initial },
  );
}

describe('useTableRows', () => {
  beforeEach(() => {
    mocks.apiGet.mockReset();
  });

  it('encodes filter, sort, offset and limit as query params', async () => {
    mocks.apiGet.mockResolvedValue(page(5));
    const filter = { column: 'Status', op: 'eq' as const, value: 'open' };
    const sort = [{ column: 'Name', direction: 'desc' as const }];
    const { result } = setup({ filter, sort, offset: 200, limit: 200 });
    await waitFor(() => expect(result.current.page?.total).toBe(5));
    expect(mocks.apiGet).toHaveBeenCalledTimes(1);
    const [url, opts] = mocks.apiGet.mock.calls[0];
    expect(url).toBe('/app/api/tables/t1/rows');
    expect(opts?.query).toEqual({
      filter: JSON.stringify(filter),
      sort: JSON.stringify(sort),
      limit: 200,
      offset: 200,
      count_only: undefined,
    });
  });

  it('defaults to a 200-row first page with no filter or sort', async () => {
    mocks.apiGet.mockResolvedValue(page(1));
    const { result } = setup();
    await waitFor(() => expect(result.current.page).not.toBeNull());
    expect(mocks.apiGet.mock.calls[0][1]?.query).toMatchObject({
      filter: undefined, sort: undefined, limit: 200, offset: 0,
    });
  });

  it('does not refetch for equal params in new objects, but does for a new offset', async () => {
    mocks.apiGet.mockResolvedValue(page(1));
    const { result, rerender } = setup({ sort: [{ column: 'A', direction: 'asc' }] });
    await waitFor(() => expect(result.current.loading).toBe(false));
    rerender({ sort: [{ column: 'A', direction: 'asc' }] });
    expect(mocks.apiGet).toHaveBeenCalledTimes(1);
    rerender({ sort: [{ column: 'A', direction: 'asc' }], offset: 200 });
    await waitFor(() => expect(mocks.apiGet).toHaveBeenCalledTimes(2));
  });

  it('refetches when refreshKey changes and keeps the previous page meanwhile', async () => {
    mocks.apiGet.mockResolvedValueOnce(page(3, 0, 'first'));
    const { result, rerender } = setup({ refreshKey: 0 });
    await waitFor(() => expect(result.current.page?.rows[0].c_a).toBe('first'));
    const next = deferred<TableRowsPage>();
    mocks.apiGet.mockReturnValueOnce(next.promise);
    rerender({ refreshKey: 1 });
    await waitFor(() => expect(result.current.loading).toBe(true));
    expect(result.current.page?.rows[0].c_a).toBe('first');
    await act(async () => next.resolve(page(4, 0, 'second')));
    expect(result.current.page?.rows[0].c_a).toBe('second');
    expect(result.current.loading).toBe(false);
  });

  it('drops an older response that lands after a newer one', async () => {
    const slow = deferred<TableRowsPage>();
    const fast = deferred<TableRowsPage>();
    mocks.apiGet.mockReturnValueOnce(slow.promise).mockReturnValueOnce(fast.promise);
    const { result, rerender } = setup({ offset: 0 });
    rerender({ offset: 200 });
    await act(async () => fast.resolve(page(400, 200, 'new')));
    expect(result.current.page?.rows[0].c_a).toBe('new');
    await act(async () => slow.resolve(page(400, 0, 'old')));
    expect(result.current.page?.rows[0].c_a).toBe('new');
    expect(result.current.loading).toBe(false);
  });

  it('coalesces a burst of refreshKey bumps into one follow-up fetch', async () => {
    const first = deferred<TableRowsPage>();
    mocks.apiGet.mockReturnValueOnce(first.promise);
    const { result, rerender } = setup({ refreshKey: 0 });
    rerender({ refreshKey: 1 });
    rerender({ refreshKey: 2 });
    rerender({ refreshKey: 3 });
    // The in-flight fetch is not cancelled by the bumps.
    expect(mocks.apiGet).toHaveBeenCalledTimes(1);
    mocks.apiGet.mockResolvedValueOnce(page(9, 0, 'latest'));
    await act(async () => first.resolve(page(8, 0, 'first')));
    await waitFor(() => expect(result.current.page?.rows[0].c_a).toBe('latest'));
    expect(mocks.apiGet).toHaveBeenCalledTimes(2);
    expect(result.current.loading).toBe(false);
  });

  it('retries a transient failure before reporting it', async () => {
    mocks.apiGet.mockRejectedValueOnce(new ApiClientError('busy', 503, 'busy'));
    mocks.apiGet.mockResolvedValueOnce(page(4));
    const { result } = setup({});
    await waitFor(() => expect(result.current.page?.total).toBe(4), { timeout: 3000 });
    expect(mocks.apiGet).toHaveBeenCalledTimes(2);
    expect(result.current.error).toBeNull();
  });

  it('keeps the ApiClientError (with its filter path) and the last good page', async () => {
    mocks.apiGet.mockResolvedValueOnce(page(2));
    const { result, rerender } = setup({});
    await waitFor(() => expect(result.current.page).not.toBeNull());
    const err = new ApiClientError('Unknown column', 400, 'unknown_column', undefined, {
      detail: { error: 'unknown_column', message: 'Unknown column', path: 'and[1].column' },
    });
    mocks.apiGet.mockRejectedValueOnce(err);
    rerender({ filter: { and: [{ column: 'A', op: 'is_null' }, { column: 'Nope', op: 'is_null' }] } });
    await waitFor(() => expect(result.current.error).toBe(err));
    expect(filterErrorPath(result.current.error)).toBe('and[1].column');
    expect(result.current.page?.total).toBe(2);
    mocks.apiGet.mockResolvedValueOnce(page(1));
    rerender({});
    await waitFor(() => expect(result.current.error).toBeNull());
  });

  it('drops the page when another table opens, and fetches nothing while disabled', async () => {
    mocks.apiGet.mockResolvedValueOnce(page(2));
    const { result, rerender } = setup({ tableId: 't1' });
    await waitFor(() => expect(result.current.page).not.toBeNull());
    const other = deferred<TableRowsPage>();
    mocks.apiGet.mockReturnValueOnce(other.promise);
    rerender({ tableId: 't2' });
    expect(result.current.page).toBeNull();
    expect(mocks.apiGet.mock.calls[1][0]).toBe('/app/api/tables/t2/rows');
    rerender({ tableId: 't3', enabled: false });
    expect(mocks.apiGet).toHaveBeenCalledTimes(2);
    await act(async () => other.resolve(page(7)));
    expect(result.current.page).toBeNull();
    expect(result.current.loading).toBe(false);
  });
});
