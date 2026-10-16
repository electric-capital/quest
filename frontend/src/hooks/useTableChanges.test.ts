// useTableChanges: first page, keyset Load more with before_id, re-list on
// this table's table_changed (debounced with a max wait, coalesced while a
// fetch is in flight), Load more and refresh serialized (neither loses the
// other's work), other tables ignored, out-of-order responses dropped, 403
// and 404 table_not_found -> forbidden, Retry after a failed first load,
// addChange.
import { act, renderHook, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiClientError } from '../api/request';
import type { ListTableChangesResponse, TableChange } from '../api/types';
import {
  TABLE_CHANGES_REFRESH_DEBOUNCE_MS,
  TABLE_CHANGES_REFRESH_MAX_WAIT_MS,
  useTableChanges,
} from './useTableChanges';

type GlobalListener = (event: { type: string; [key: string]: unknown }) => void;

const mocks = vi.hoisted(() => ({
  listTableChanges: vi.fn<
    (id: string, opts?: { beforeId?: number | null; limit?: number }) => Promise<ListTableChangesResponse>
  >(),
  global: new Set<(event: { type: string; [key: string]: unknown }) => void>(),
}));

vi.mock('../api/tablesApi', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/tablesApi')>()),
  listTableChanges: mocks.listTableChanges,
}));

vi.mock('../services/PersistentWebSocket', () => ({
  persistentWebSocket: {
    onGlobalEvent: (cb: GlobalListener) => {
      mocks.global.add(cb);
      return () => mocks.global.delete(cb);
    },
  },
}));

function change(id: number, overrides: Partial<TableChange> = {}): TableChange {
  return {
    id, created_at: '2026-10-01T12:00:00.000000+00:00', source: 'ui:1', source_label: 'you', conversation: null,
    actor: { user_id: 1, name: 'Ada' }, kind: 'rows', op: 'insert', summary: `Change ${id}`,
    row_count: 1, in_effect: true, restore_to: null, schema_before: 1, schema_after: 1, size_bytes: 10,
    ...overrides,
  };
}

function page(ids: number[], hasMore: boolean): ListTableChangesResponse {
  return {
    changes: ids.map((id) => change(id)),
    has_more: hasMore,
    next_before_id: hasMore ? ids[ids.length - 1] : null,
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

function emit(event: { type: string; [key: string]: unknown }) {
  for (const cb of mocks.global) cb(event);
}

beforeEach(() => {
  mocks.listTableChanges.mockReset();
  mocks.global.clear();
});

afterEach(() => {
  vi.useRealTimers();
});

describe('useTableChanges', () => {
  it('loads the first page, then pages on with before_id', async () => {
    mocks.listTableChanges.mockResolvedValueOnce(page([5, 4], true));
    const { result } = renderHook(() => useTableChanges('t1', { limit: 2 }));
    expect(result.current.loading).toBe(true);
    await waitFor(() => expect(result.current.changes.map((c) => c.id)).toEqual([5, 4]));
    expect(mocks.listTableChanges).toHaveBeenCalledWith('t1', { limit: 2 });
    expect(result.current.hasMore).toBe(true);

    mocks.listTableChanges.mockResolvedValueOnce(page([3, 2], false));
    act(() => result.current.loadMore());
    await waitFor(() => expect(result.current.changes.map((c) => c.id)).toEqual([5, 4, 3, 2]));
    expect(mocks.listTableChanges).toHaveBeenLastCalledWith('t1', { beforeId: 4, limit: 2 });
    expect(result.current.hasMore).toBe(false);
  });

  it('re-lists the loaded window on a table_changed for this table, debounced', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    mocks.listTableChanges.mockResolvedValueOnce(page([5, 4], true));
    const { result } = renderHook(() => useTableChanges('t1', { limit: 2 }));
    await waitFor(() => expect(result.current.changes).toHaveLength(2));
    mocks.listTableChanges.mockResolvedValueOnce(page([3, 2], false));
    act(() => result.current.loadMore());
    await waitFor(() => expect(result.current.changes).toHaveLength(4));

    mocks.listTableChanges.mockResolvedValue(page([6, 5, 4, 3, 2], false));
    act(() => {
      emit({ type: 'table_changed', table_id: 't1', updated_at: 'x', schema_version: 1 });
      emit({ type: 'table_changed', table_id: 't1', updated_at: 'y', schema_version: 1 });
    });
    expect(mocks.listTableChanges).toHaveBeenCalledTimes(2);
    await act(async () => {
      vi.advanceTimersByTime(TABLE_CHANGES_REFRESH_DEBOUNCE_MS + 10);
    });
    await waitFor(() => expect(result.current.changes.map((c) => c.id)).toEqual([6, 5, 4, 3, 2]));
    // One coalesced re-fetch of the whole loaded window (4 entries).
    expect(mocks.listTableChanges).toHaveBeenCalledTimes(3);
    expect(mocks.listTableChanges).toHaveBeenLastCalledWith('t1', { limit: 4 });
  });

  it('ignores table_changed for other tables and other event types', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    mocks.listTableChanges.mockResolvedValue(page([1], false));
    const { result } = renderHook(() => useTableChanges('t1'));
    await waitFor(() => expect(result.current.changes).toHaveLength(1));
    act(() => {
      emit({ type: 'table_changed', table_id: 't2', updated_at: 'x', schema_version: 1 });
      emit({ type: 'table_list_changed' });
    });
    await act(async () => {
      vi.advanceTimersByTime(TABLE_CHANGES_REFRESH_DEBOUNCE_MS * 3);
    });
    expect(mocks.listTableChanges).toHaveBeenCalledTimes(1);
  });

  it('runs one more fetch when an event lands while a refresh is in flight', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    mocks.listTableChanges.mockResolvedValueOnce(page([1], false));
    const { result } = renderHook(() => useTableChanges('t1'));
    await waitFor(() => expect(result.current.changes).toHaveLength(1));

    const slow = deferred<ListTableChangesResponse>();
    mocks.listTableChanges.mockReturnValueOnce(slow.promise);
    act(() => void result.current.refresh());
    expect(mocks.listTableChanges).toHaveBeenCalledTimes(2);
    act(() => emit({ type: 'table_changed', table_id: 't1', updated_at: 'x', schema_version: 1 }));
    await act(async () => {
      vi.advanceTimersByTime(TABLE_CHANGES_REFRESH_DEBOUNCE_MS + 10);
    });
    // Not superseded: still waiting on the slow one.
    expect(mocks.listTableChanges).toHaveBeenCalledTimes(2);
    mocks.listTableChanges.mockResolvedValueOnce(page([3, 2, 1], false));
    await act(async () => slow.resolve(page([2, 1], false)));
    await waitFor(() => expect(result.current.changes.map((c) => c.id)).toEqual([3, 2, 1]));
    expect(mocks.listTableChanges).toHaveBeenCalledTimes(3);
  });

  it('drops a response for a table no longer shown', async () => {
    const first = deferred<ListTableChangesResponse>();
    mocks.listTableChanges.mockReturnValueOnce(first.promise);
    mocks.listTableChanges.mockResolvedValueOnce(page([9], false));
    const { result, rerender } = renderHook(({ id }) => useTableChanges(id), {
      initialProps: { id: 't1' },
    });
    rerender({ id: 't2' });
    await waitFor(() => expect(result.current.changes.map((c) => c.id)).toEqual([9]));
    await act(async () => first.resolve(page([1], false)));
    expect(result.current.changes.map((c) => c.id)).toEqual([9]);
  });

  it('sets forbidden on a 403 and stops fetching', async () => {
    mocks.listTableChanges.mockRejectedValueOnce(
      new ApiClientError('History is available to people who can edit this table.', 403, 'forbidden'),
    );
    const { result } = renderHook(() => useTableChanges('t1'));
    await waitFor(() => expect(result.current.forbidden).toBe(true));
    expect(result.current.loading).toBe(false);
    expect(result.current.changes).toEqual([]);
  });

  it('reports a failed first load as error', async () => {
    mocks.listTableChanges.mockRejectedValueOnce(new ApiClientError('Boom', 500));
    const { result } = renderHook(() => useTableChanges('t1'));
    await waitFor(() => expect(result.current.error).toBe('Boom'));
    expect(result.current.forbidden).toBe(false);
  });

  it('re-lists within the max wait under a steady stream of events', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    mocks.listTableChanges.mockResolvedValue(page([1], false));
    const { result } = renderHook(() => useTableChanges('t1'));
    await waitFor(() => expect(result.current.changes).toHaveLength(1));
    // One event per (debounce - 50) ms: the quiet period never comes.
    const step = TABLE_CHANGES_REFRESH_DEBOUNCE_MS - 50;
    let elapsed = 0;
    while (elapsed < TABLE_CHANGES_REFRESH_MAX_WAIT_MS) {
      act(() => emit({ type: 'table_changed', table_id: 't1', updated_at: 'x', schema_version: 1 }));
      await act(async () => {
        vi.advanceTimersByTime(step);
      });
      elapsed += step;
    }
    // Due at the max wait, while the last event's quiet period still runs.
    expect(mocks.listTableChanges).toHaveBeenCalledTimes(2);
  });

  it('holds Load more while a refresh is in flight, then pages on from the new window', async () => {
    mocks.listTableChanges.mockResolvedValueOnce(page([5, 4], true));
    const { result } = renderHook(() => useTableChanges('t1', { limit: 2 }));
    await waitFor(() => expect(result.current.changes).toHaveLength(2));

    const slow = deferred<ListTableChangesResponse>();
    mocks.listTableChanges.mockReturnValueOnce(slow.promise);
    act(() => void result.current.refresh());
    act(() => result.current.loadMore());
    expect(result.current.loadingMore).toBe(true);
    // The page waits for the window fetch.
    expect(mocks.listTableChanges).toHaveBeenCalledTimes(2);

    // A write landed meanwhile: the window now ends at #5, not #4.
    mocks.listTableChanges.mockResolvedValueOnce(page([4, 3], false));
    await act(async () => slow.resolve(page([6, 5], true)));
    await waitFor(() => expect(result.current.changes.map((c) => c.id)).toEqual([6, 5, 4, 3]));
    expect(mocks.listTableChanges).toHaveBeenLastCalledWith('t1', { beforeId: 5, limit: 2 });
    expect(result.current.loadingMore).toBe(false);
    expect(result.current.hasMore).toBe(false);
  });

  it('runs a refresh asked for while a page loads after it, with the new count', async () => {
    mocks.listTableChanges.mockResolvedValueOnce(page([5, 4], true));
    const { result } = renderHook(() => useTableChanges('t1', { limit: 2 }));
    await waitFor(() => expect(result.current.changes).toHaveLength(2));

    const slowPage = deferred<ListTableChangesResponse>();
    mocks.listTableChanges.mockReturnValueOnce(slowPage.promise);
    act(() => result.current.loadMore());
    act(() => void result.current.refresh());
    expect(mocks.listTableChanges).toHaveBeenCalledTimes(2);

    const windowFetch = deferred<ListTableChangesResponse>();
    mocks.listTableChanges.mockReturnValueOnce(windowFetch.promise);
    await act(async () => slowPage.resolve(page([3, 2], true)));
    // The page is appended, and the refresh covers it (4 entries).
    expect(result.current.changes.map((c) => c.id)).toEqual([5, 4, 3, 2]);
    expect(mocks.listTableChanges).toHaveBeenCalledTimes(3);
    expect(mocks.listTableChanges).toHaveBeenLastCalledWith('t1', { limit: 4 });
    await act(async () => windowFetch.resolve(page([6, 5, 4, 3], true)));
    expect(result.current.changes.map((c) => c.id)).toEqual([6, 5, 4, 3]);
  });

  it('goes back to loading on a retry after a failed first load', async () => {
    mocks.listTableChanges.mockRejectedValueOnce(new ApiClientError('Boom', 500));
    const { result } = renderHook(() => useTableChanges('t1'));
    await waitFor(() => expect(result.current.error).toBe('Boom'));

    const retry = deferred<ListTableChangesResponse>();
    mocks.listTableChanges.mockReturnValueOnce(retry.promise);
    act(() => void result.current.refresh());
    expect(result.current.loading).toBe(true);
    expect(result.current.error).toBeNull();
    await act(async () => retry.reject(new ApiClientError('Boom again', 500)));
    expect(result.current.loading).toBe(false);
    expect(result.current.error).toBe('Boom again');
  });

  it('sets forbidden on a 404 table_not_found', async () => {
    mocks.listTableChanges.mockRejectedValueOnce(new ApiClientError('Table not found.', 404, 'table_not_found'));
    const { result } = renderHook(() => useTableChanges('t1'));
    await waitFor(() => expect(result.current.forbidden).toBe(true));
    expect(result.current.error).toBeNull();
  });

  it('addChange puts a newer entry on top and ignores older ones', async () => {
    mocks.listTableChanges.mockResolvedValueOnce(page([5, 4], true));
    const { result } = renderHook(() => useTableChanges('t1', { limit: 2 }));
    await waitFor(() => expect(result.current.changes).toHaveLength(2));
    act(() => result.current.addChange(change(6, { kind: 'restore', restore_to: 4 })));
    act(() => result.current.addChange(change(5)));
    act(() => result.current.addChange(change(3)));
    expect(result.current.changes.map((c) => c.id)).toEqual([6, 5, 4]);
    expect(result.current.hasMore).toBe(true);
  });

  it('fetches nothing while disabled', () => {
    renderHook(() => useTableChanges('t1', { enabled: false }));
    expect(mocks.listTableChanges).not.toHaveBeenCalled();
  });
});
