import { act, renderHook, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiClientError } from '../api/request';
import type { Doc, DocDetail } from '../api/types';
import { useDoc } from './useDoc';

type GlobalListener = (event: { type: string; [key: string]: unknown }) => void;

const mocks = vi.hoisted(() => ({
  fetchDoc: vi.fn<(id: string) => Promise<DocDetail>>(),
  global: new Set<(event: { type: string; [key: string]: unknown }) => void>(),
}));

vi.mock('../api/docsApi', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/docsApi')>()),
  fetchDoc: mocks.fetchDoc,
}));

vi.mock('../services/PersistentWebSocket', () => ({
  persistentWebSocket: {
    onGlobalEvent: (cb: GlobalListener) => {
      mocks.global.add(cb);
      return () => mocks.global.delete(cb);
    },
  },
}));

function row(id: string, overrides: Partial<Doc> = {}): Doc {
  return {
    id,
    owner_id: 1,
    project_id: null,
    title: `Doc ${id}`,
    description: '',
    mode: 'private',
    content_size: 5,
    asset_count: 0,
    last_write_source: 'ui',
    created_at: '2026-10-01T00:00:00',
    updated_at: '2026-10-01T00:00:00',
    scope: 'user',
    shared: false,
    access: { can_rename: true, can_switch_mode: true, can_delete: true, write: 'free' },
    ...overrides,
  };
}

function detail(id: string, overrides: Partial<DocDetail> = {}): DocDetail {
  return { ...row(id), content: `# Body of ${id}`, last_write_conversation: null, ...overrides };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((res) => {
    resolve = res;
  });
  return { promise, resolve };
}

function emit(event: { type: string; [key: string]: unknown }) {
  for (const cb of mocks.global) cb(event);
}

const notFoundError = () => new ApiClientError('x', 404, 'doc_not_found');

describe('useDoc', () => {
  beforeEach(() => {
    mocks.fetchDoc.mockReset();
    mocks.global.clear();
  });

  it('loads the doc', async () => {
    mocks.fetchDoc.mockResolvedValueOnce(detail('d1'));
    const { result } = renderHook(() => useDoc('d1'));

    expect(result.current.loading).toBe(true);
    expect(result.current.doc).toBeNull();
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(mocks.fetchDoc).toHaveBeenCalledWith('d1');
    expect(result.current.doc?.content).toBe('# Body of d1');
    expect(result.current.notFound).toBe(false);
    expect(result.current.error).toBeNull();
  });

  it('does nothing for a null id', async () => {
    const { result } = renderHook(() => useDoc(null));
    expect(result.current.loading).toBe(false);
    expect(result.current.doc).toBeNull();
    await act(async () => {
      await result.current.refresh();
    });
    expect(mocks.fetchDoc).not.toHaveBeenCalled();
    expect(mocks.global.size).toBe(0);
  });

  it('reports notFound on a 404', async () => {
    mocks.fetchDoc.mockRejectedValueOnce(notFoundError());
    const { result } = renderHook(() => useDoc('missing'));
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.notFound).toBe(true);
    expect(result.current.doc).toBeNull();
    expect(result.current.error).toBeNull();
  });

  it('reports other failures as an ApiClientError', async () => {
    mocks.fetchDoc.mockRejectedValueOnce(new ApiClientError('Quest Docs is off', 403, 'docs_disabled'));
    const { result } = renderHook(() => useDoc('d1'));
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.notFound).toBe(false);
    expect(result.current.error?.errorCode).toBe('docs_disabled');

    mocks.fetchDoc.mockRejectedValueOnce(new TypeError('network down'));
    const { result: other } = renderHook(() => useDoc('d2'));
    await waitFor(() => expect(other.current.loading).toBe(false));
    expect(other.current.error).toBeInstanceOf(ApiClientError);
    expect(other.current.error?.message).toBe('network down');
  });

  it('re-fetches on doc_changed only for this doc with a different updated_at', async () => {
    mocks.fetchDoc.mockResolvedValueOnce(detail('d1', { updated_at: '2026-10-01T00:00:00' }));
    const { result } = renderHook(() => useDoc('d1'));
    await waitFor(() => expect(result.current.loading).toBe(false));

    act(() => emit({ type: 'doc_changed', doc_id: 'd1', updated_at: '2026-10-01T00:00:00' }));
    act(() => emit({ type: 'doc_changed', doc_id: 'other', updated_at: '2026-10-02T00:00:00' }));
    act(() => emit({ type: 'request_count_changed' }));
    expect(mocks.fetchDoc).toHaveBeenCalledTimes(1);

    mocks.fetchDoc.mockResolvedValueOnce(
      detail('d1', { updated_at: '2026-10-02T00:00:00', content: 'new body' }),
    );
    await act(async () => {
      emit({ type: 'doc_changed', doc_id: 'd1', updated_at: '2026-10-02T00:00:00' });
    });
    await waitFor(() => expect(result.current.doc?.content).toBe('new body'));
    expect(mocks.fetchDoc).toHaveBeenCalledTimes(2);
    expect(result.current.loading).toBe(false);
  });

  it('turns a delete into notFound through doc_list_changed', async () => {
    mocks.fetchDoc.mockResolvedValueOnce(detail('d1'));
    const { result } = renderHook(() => useDoc('d1'));
    await waitFor(() => expect(result.current.loading).toBe(false));

    mocks.fetchDoc.mockRejectedValueOnce(notFoundError());
    await act(async () => {
      emit({ type: 'doc_list_changed' });
    });
    await waitFor(() => expect(result.current.notFound).toBe(true));
    expect(result.current.doc).toBeNull();
  });

  it('keeps the shown doc when a background refresh fails', async () => {
    mocks.fetchDoc.mockResolvedValueOnce(detail('d1'));
    const { result } = renderHook(() => useDoc('d1'));
    await waitFor(() => expect(result.current.loading).toBe(false));

    mocks.fetchDoc.mockRejectedValueOnce(new ApiClientError('boom', 500));
    await act(async () => {
      await result.current.refresh();
    });
    expect(result.current.doc?.id).toBe('d1');
    expect(result.current.error).toBeNull();
  });

  it('applyRow merges a rename / mode row and keeps the content', async () => {
    mocks.fetchDoc.mockResolvedValueOnce(detail('d1'));
    const { result } = renderHook(() => useDoc('d1'));
    await waitFor(() => expect(result.current.loading).toBe(false));

    act(() =>
      result.current.applyRow(
        row('d1', { title: 'Renamed', mode: 'public', updated_at: '2026-10-03T00:00:00' }),
      ),
    );
    expect(result.current.doc?.title).toBe('Renamed');
    expect(result.current.doc?.mode).toBe('public');
    expect(result.current.doc?.content).toBe('# Body of d1');
    // No fetch in flight, so nothing to re-fetch.
    expect(mocks.fetchDoc).toHaveBeenCalledTimes(1);

    // The rename's own doc_changed echo is skipped; a row for another doc is ignored.
    act(() => emit({ type: 'doc_changed', doc_id: 'd1', updated_at: '2026-10-03T00:00:00' }));
    act(() => result.current.applyRow(row('d2', { title: 'Elsewhere' })));
    expect(mocks.fetchDoc).toHaveBeenCalledTimes(1);
    expect(result.current.doc?.title).toBe('Renamed');
  });

  it('applyRow ignores a row older than the one shown and re-fetches when the body changed', async () => {
    mocks.fetchDoc.mockResolvedValueOnce(detail('d1', { updated_at: '2026-10-05T00:00:00' }));
    const { result } = renderHook(() => useDoc('d1'));
    await waitFor(() => expect(result.current.loading).toBe(false));

    // An overtaken rename response must not roll the title/token back.
    act(() => result.current.applyRow(row('d1', { title: 'Older', updated_at: '2026-10-02T00:00:00' })));
    expect(result.current.doc?.title).toBe('Doc d1');
    expect(mocks.fetchDoc).toHaveBeenCalledTimes(1);

    // A newer row whose size differs (a model write this tab missed) is
    // applied AND the body is re-fetched.
    mocks.fetchDoc.mockResolvedValueOnce(
      detail('d1', { content_size: 99, updated_at: '2026-10-06T00:00:00', content: 'fresh' }),
    );
    await act(async () => {
      result.current.applyRow(row('d1', { content_size: 99, updated_at: '2026-10-06T00:00:00' }));
    });
    await waitFor(() => expect(result.current.doc?.content).toBe('fresh'));
    expect(mocks.fetchDoc).toHaveBeenCalledTimes(2);
  });

  it('applyRow re-fetches when an older fetch is in flight so it cannot revert the row', async () => {
    mocks.fetchDoc.mockResolvedValueOnce(detail('d1'));
    const { result } = renderHook(() => useDoc('d1'));
    await waitFor(() => expect(result.current.loading).toBe(false));

    const stale = deferred<DocDetail>();
    mocks.fetchDoc.mockReturnValueOnce(stale.promise);
    act(() => emit({ type: 'doc_list_changed' }));

    mocks.fetchDoc.mockResolvedValueOnce(
      detail('d1', { title: 'Renamed', updated_at: '2026-10-03T00:00:00', content: 'fresh' }),
    );
    await act(async () => {
      result.current.applyRow(row('d1', { title: 'Renamed', updated_at: '2026-10-03T00:00:00' }));
    });
    await waitFor(() => expect(result.current.doc?.content).toBe('fresh'));

    await act(async () => {
      stale.resolve(detail('d1', { title: 'Old title' }));
      await stale.promise;
    });
    expect(result.current.doc?.title).toBe('Renamed');
    expect(mocks.fetchDoc).toHaveBeenCalledTimes(3);
  });

  it('drops a previous doc response after the id changes', async () => {
    const first = deferred<DocDetail>();
    mocks.fetchDoc.mockReturnValueOnce(first.promise).mockResolvedValueOnce(detail('d2'));
    const { result, rerender } = renderHook((id: string | null) => useDoc(id), {
      initialProps: 'd1' as string | null,
    });
    rerender('d2');
    expect(result.current.loading).toBe(true);
    await waitFor(() => expect(result.current.doc?.id).toBe('d2'));

    await act(async () => {
      first.resolve(detail('d1'));
      await first.promise;
    });
    expect(result.current.doc?.id).toBe('d2');

    // Closing the viewer clears the doc.
    rerender(null);
    expect(result.current.doc).toBeNull();
    expect(result.current.loading).toBe(false);
  });

  it('unsubscribes from the realtime layer on unmount', async () => {
    mocks.fetchDoc.mockResolvedValueOnce(detail('d1'));
    const { result, unmount } = renderHook(() => useDoc('d1'));
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(mocks.global.size).toBe(1);
    unmount();
    expect(mocks.global.size).toBe(0);
  });
});
