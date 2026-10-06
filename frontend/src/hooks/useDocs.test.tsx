import { act, renderHook, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { FetchDocsOptions } from '../api/docsApi';
import { ApiClientError } from '../api/request';
import type { Doc, ListDocsResponse } from '../api/types';
import { useDocs, type UseDocsOptions } from './useDocs';

type GlobalListener = (event: { type: string; [key: string]: unknown }) => void;

const mocks = vi.hoisted(() => ({
  fetchDocs: vi.fn<(opts: FetchDocsOptions) => Promise<ListDocsResponse>>(),
  global: new Set<(event: { type: string; [key: string]: unknown }) => void>(),
}));

vi.mock('../api/docsApi', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/docsApi')>()),
  fetchDocs: mocks.fetchDocs,
}));

vi.mock('../services/PersistentWebSocket', () => ({
  persistentWebSocket: {
    onGlobalEvent: (cb: GlobalListener) => {
      mocks.global.add(cb);
      return () => mocks.global.delete(cb);
    },
  },
}));

function doc(id: string, overrides: Partial<Doc> = {}): Doc {
  return {
    id,
    owner_id: 1,
    project_id: null,
    title: `Doc ${id}`,
    description: '',
    mode: 'private',
    content_size: 0,
    asset_count: 0,
    last_write_source: 'ui',
    created_at: '2026-10-01T00:00:00',
    updated_at: '2026-10-01T00:00:00',
    scope: 'user',
    shared: false,
    access: { can_rename: true, can_switch_mode: false, can_delete: true, write: 'free' },
    ...overrides,
  };
}

function page(ids: string[], nextCursor: string | null = null): ListDocsResponse {
  return { docs: ids.map((id) => doc(id)), has_more: nextCursor !== null, next_cursor: nextCursor };
}

/** A promise the test resolves by hand, to inspect in-flight state. */
function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function emit(type: string) {
  for (const cb of mocks.global) cb({ type });
}

describe('useDocs', () => {
  beforeEach(() => {
    mocks.fetchDocs.mockReset();
    mocks.global.clear();
  });

  it('loads the first page, appends the next one by cursor, and stops at has_more false', async () => {
    mocks.fetchDocs.mockResolvedValueOnce(page(['a', 'b'], 'cursor-1'));
    const { result } = renderHook(() => useDocs({ limit: 2 }));

    expect(result.current.loading).toBe(true);
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(mocks.fetchDocs).toHaveBeenCalledWith({ projectId: null, limit: 2 });
    expect(result.current.docs.map((d) => d.id)).toEqual(['a', 'b']);
    expect(result.current.hasMore).toBe(true);
    expect(result.current.error).toBeNull();

    // The next page overlaps on one row (bumped since page one): deduped.
    const next = deferred<ListDocsResponse>();
    mocks.fetchDocs.mockReturnValueOnce(next.promise);
    act(() => result.current.loadMore());
    expect(result.current.loadingMore).toBe(true);
    expect(result.current.loading).toBe(false);
    expect(mocks.fetchDocs).toHaveBeenLastCalledWith({ projectId: null, limit: 2, cursor: 'cursor-1' });
    await act(async () => {
      next.resolve(page(['b', 'c'], null));
      await next.promise;
    });

    expect(result.current.loadingMore).toBe(false);
    expect(result.current.docs.map((d) => d.id)).toEqual(['a', 'b', 'c']);
    expect(result.current.hasMore).toBe(false);

    // Nothing left: loadMore is a no-op.
    act(() => result.current.loadMore());
    expect(mocks.fetchDocs).toHaveBeenCalledTimes(2);
  });

  it('treats a cursor without has_more as the end of the list', async () => {
    mocks.fetchDocs.mockResolvedValueOnce({ docs: [doc('a')], has_more: false, next_cursor: 'x' });
    const { result } = renderHook(() => useDocs({ limit: 5 }));
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.hasMore).toBe(false);
  });

  it('passes the project id through and defaults to 50 rows per page', async () => {
    mocks.fetchDocs.mockResolvedValueOnce(page(['p1']));
    const { result } = renderHook(() => useDocs({ projectId: 'proj-1' }));
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(mocks.fetchDocs).toHaveBeenCalledWith({ projectId: 'proj-1', limit: 50 });
  });

  it('refreshes the loaded window silently on doc_list_changed and ignores other events', async () => {
    mocks.fetchDocs.mockResolvedValueOnce(page(['a', 'b'], 'cursor-1'));
    const { result } = renderHook(() => useDocs({ limit: 2 }));
    await waitFor(() => expect(result.current.loading).toBe(false));
    mocks.fetchDocs.mockResolvedValueOnce(page(['c'], null));
    act(() => result.current.loadMore());
    await waitFor(() => expect(result.current.docs).toHaveLength(3));

    act(() => emit('conversation_list_changed'));
    expect(mocks.fetchDocs).toHaveBeenCalledTimes(2);

    // One request spanning everything loaded, no loading flash.
    const pending = deferred<ListDocsResponse>();
    mocks.fetchDocs.mockReturnValueOnce(pending.promise);
    act(() => emit('doc_list_changed'));
    expect(mocks.fetchDocs).toHaveBeenLastCalledWith({ projectId: null, limit: 3 });
    expect(result.current.loading).toBe(false);
    expect(result.current.docs.map((d) => d.id)).toEqual(['a', 'b', 'c']);

    await act(async () => {
      pending.resolve(page(['new', 'a', 'b'], 'cursor-2'));
      await pending.promise;
    });
    expect(result.current.docs.map((d) => d.id)).toEqual(['new', 'a', 'b']);
    expect(result.current.hasMore).toBe(true);
  });

  it('keeps the list when a background refresh fails', async () => {
    mocks.fetchDocs.mockResolvedValueOnce(page(['a']));
    const { result } = renderHook(() => useDocs({ limit: 5 }));
    await waitFor(() => expect(result.current.loading).toBe(false));

    mocks.fetchDocs.mockRejectedValueOnce(new ApiClientError('boom', 500));
    await act(async () => {
      await result.current.refresh();
    });
    expect(result.current.docs.map((d) => d.id)).toEqual(['a']);
    expect(result.current.error).toBeNull();
  });

  it('surfaces a failed first load and clears it on the next successful refresh', async () => {
    mocks.fetchDocs.mockRejectedValueOnce(new ApiClientError('Project not found', 404, 'project_not_found'));
    const { result } = renderHook(() => useDocs({ projectId: 'gone', limit: 5 }));
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.error).toBe('Project not found');
    expect(result.current.docs).toEqual([]);

    mocks.fetchDocs.mockRejectedValueOnce(new TypeError('network'));
    const { result: other } = renderHook(() => useDocs({ limit: 5 }));
    await waitFor(() => expect(other.current.loading).toBe(false));
    expect(other.current.error).toBe('Failed to load docs');

    mocks.fetchDocs.mockResolvedValueOnce(page(['a']));
    await act(async () => {
      await other.current.refresh();
    });
    expect(other.current.error).toBeNull();
    expect(other.current.docs.map((d) => d.id)).toEqual(['a']);
  });

  it('fetches nothing and subscribes to nothing while disabled', async () => {
    mocks.fetchDocs.mockResolvedValue(page(['a']));
    const { result, rerender } = renderHook((opts: UseDocsOptions) => useDocs(opts), {
      initialProps: { limit: 5, enabled: false },
    });

    expect(result.current.loading).toBe(false);
    expect(result.current.docs).toEqual([]);
    expect(result.current.hasMore).toBe(false);
    act(() => emit('doc_list_changed'));
    await act(async () => {
      await result.current.refresh();
    });
    expect(mocks.fetchDocs).not.toHaveBeenCalled();
    expect(mocks.global.size).toBe(0);

    rerender({ limit: 5, enabled: true });
    expect(result.current.loading).toBe(true);
    await waitFor(() => expect(result.current.docs.map((d) => d.id)).toEqual(['a']));
    expect(mocks.fetchDocs).toHaveBeenCalledTimes(1);

    // Closing the gate again empties the list immediately.
    rerender({ limit: 5, enabled: false });
    expect(result.current.docs).toEqual([]);
    expect(result.current.loading).toBe(false);
  });

  it('resets on a project change and drops the older project response', async () => {
    const first = deferred<ListDocsResponse>();
    const second = deferred<ListDocsResponse>();
    mocks.fetchDocs.mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
    const { result, rerender } = renderHook((opts: UseDocsOptions) => useDocs(opts), {
      initialProps: { projectId: 'A', limit: 5 },
    });
    rerender({ projectId: 'B', limit: 5 });
    expect(mocks.fetchDocs).toHaveBeenLastCalledWith({ projectId: 'B', limit: 5 });

    await act(async () => {
      second.resolve(page(['b1']));
      await second.promise;
    });
    expect(result.current.docs.map((d) => d.id)).toEqual(['b1']);

    // Project A's response resolving late must not replace B's list.
    await act(async () => {
      first.resolve(page(['a1', 'a2'], 'cursor-a'));
      await first.promise;
    });
    expect(result.current.docs.map((d) => d.id)).toEqual(['b1']);
    expect(result.current.hasMore).toBe(false);

    // Switching back shows no stale rows while A reloads.
    const again = deferred<ListDocsResponse>();
    mocks.fetchDocs.mockReturnValueOnce(again.promise);
    rerender({ projectId: 'A', limit: 5 });
    expect(result.current.docs).toEqual([]);
    expect(result.current.loading).toBe(true);
    await act(async () => {
      again.resolve(page(['a1']));
      await again.promise;
    });
    expect(result.current.docs.map((d) => d.id)).toEqual(['a1']);
  });

  it('drops a loadMore page that a refresh superseded', async () => {
    mocks.fetchDocs.mockResolvedValueOnce(page(['a'], 'cursor-1'));
    const { result } = renderHook(() => useDocs({ limit: 1 }));
    await waitFor(() => expect(result.current.loading).toBe(false));

    const more = deferred<ListDocsResponse>();
    mocks.fetchDocs.mockReturnValueOnce(more.promise);
    act(() => result.current.loadMore());
    mocks.fetchDocs.mockResolvedValueOnce(page(['z'], 'cursor-z'));
    await act(async () => {
      await result.current.refresh();
    });
    await act(async () => {
      more.resolve(page(['b']));
      await more.promise;
    });
    expect(result.current.docs.map((d) => d.id)).toEqual(['z']);
    expect(result.current.loadingMore).toBe(false);
    expect(result.current.hasMore).toBe(true);
  });

  it('drops a loadMore page requested while a refresh was already in flight', async () => {
    mocks.fetchDocs.mockResolvedValueOnce(page(['a'], 'cursor-1'));
    const { result } = renderHook(() => useDocs({ limit: 1 }));
    await waitFor(() => expect(result.current.loading).toBe(false));

    // Refresh starts first (e.g. doc_list_changed), then the user clicks
    // "Load more" with the old cursor, then the refresh lands with a new
    // boundary, then the page lands. The page extends a cursor the list no
    // longer has, so appending it could skip the row at the new boundary.
    const refreshing = deferred<ListDocsResponse>();
    mocks.fetchDocs.mockReturnValueOnce(refreshing.promise);
    let refreshDone: Promise<void> | undefined;
    act(() => {
      refreshDone = result.current.refresh();
    });
    const more = deferred<ListDocsResponse>();
    mocks.fetchDocs.mockReturnValueOnce(more.promise);
    act(() => result.current.loadMore());
    await act(async () => {
      refreshing.resolve(page(['z'], 'cursor-z'));
      await refreshDone;
    });
    await act(async () => {
      more.resolve(page(['b']));
      await more.promise;
    });
    expect(result.current.docs.map((d) => d.id)).toEqual(['z']);
    expect(result.current.hasMore).toBe(true);
    expect(result.current.loadingMore).toBe(false);
  });

  it('caps the refresh window at 200 rows', async () => {
    const ids = Array.from({ length: 200 }, (_, i) => `d${i}`);
    mocks.fetchDocs.mockResolvedValueOnce(page(ids, 'cursor-1'));
    const { result } = renderHook(() => useDocs({ limit: 200 }));
    await waitFor(() => expect(result.current.loading).toBe(false));
    mocks.fetchDocs.mockResolvedValueOnce(page(['e0']));
    act(() => result.current.loadMore());
    await waitFor(() => expect(result.current.docs).toHaveLength(201));

    mocks.fetchDocs.mockResolvedValueOnce(page(ids, 'cursor-1'));
    await act(async () => {
      await result.current.refresh();
    });
    expect(mocks.fetchDocs).toHaveBeenLastCalledWith({ projectId: null, limit: 200 });
  });

  it('unsubscribes from the realtime layer on unmount', async () => {
    mocks.fetchDocs.mockResolvedValueOnce(page([]));
    const { result, unmount } = renderHook(() => useDocs());
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(mocks.global.size).toBe(1);
    unmount();
    expect(mocks.global.size).toBe(0);
  });
});
