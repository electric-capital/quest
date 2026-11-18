// useAutoProjectRuns: an AutoProject page's updates, paged newest first by the
// server and shown in ascending seq order; `autoproject_changed` for this
// AutoProject (and a socket reconnect) re-fetches the newest page and merges
// it, never for an AutoProject this tab is deleting.
import { act, cleanup, renderHook, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiClientError } from '../api/request';
import type { AutoProjectRun, AutoProjectRunsPage } from '../api/types';
import { useAutoProjectRuns } from './useAutoProjectRuns';

type GlobalListener = (event: { type: string; [key: string]: unknown }) => void;

const mocks = vi.hoisted(() => ({
  listAutoProjectRuns: vi.fn(),
  deleting: new Set<string>(),
  global: new Set<(event: { type: string; [key: string]: unknown }) => void>(),
  reconnect: new Set<() => void>(),
}));

vi.mock('../api/autoprojectsApi', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/autoprojectsApi')>()),
  listAutoProjectRuns: mocks.listAutoProjectRuns,
  isAutoProjectDeleting: (id: string) => mocks.deleting.has(id),
}));

vi.mock('../services/PersistentWebSocket', () => ({
  persistentWebSocket: {
    onGlobalEvent: (cb: GlobalListener) => {
      mocks.global.add(cb);
      return () => mocks.global.delete(cb);
    },
    onReconnect: (cb: () => void) => {
      mocks.reconnect.add(cb);
      return () => mocks.reconnect.delete(cb);
    },
  },
}));

function run(seq: number, overrides: Partial<AutoProjectRun> = {}): AutoProjectRun {
  return {
    id: `r${seq}`,
    seq,
    conversation_id: `c${seq}`,
    prompt: `Prompt ${seq}`,
    status: 'completed',
    summary: null,
    result: null,
    notes: null,
    handoff_missing: false,
    error: null,
    model: null,
    started_at: '2026-11-10T10:00:00',
    finished_at: null,
    questions: [],
    open_requests: [],
    ...overrides,
  };
}

/** A server page: newest first, as GET /runs returns it. */
function page(seqs: number[], nextBeforeSeq: number | null = null): AutoProjectRunsPage {
  return { runs: [...seqs].sort((a, b) => b - a).map((s) => run(s)), next_before_seq: nextBeforeSeq };
}

function emit(type: string, extra: Record<string, unknown> = {}) {
  act(() => {
    for (const cb of [...mocks.global]) cb({ type, ...extra });
  });
}

function reconnect() {
  act(() => {
    for (const cb of [...mocks.reconnect]) cb();
  });
}

const seqs = (runs: AutoProjectRun[]) => runs.map((r) => r.seq);

beforeEach(() => {
  mocks.listAutoProjectRuns.mockReset();
  mocks.deleting.clear();
  mocks.global.clear();
  mocks.reconnect.clear();
});

afterEach(cleanup);

describe('useAutoProjectRuns', () => {
  it('shows the newest page in ascending order and prepends older pages by cursor', async () => {
    mocks.listAutoProjectRuns.mockResolvedValueOnce(page([5, 4, 3], 3));
    const { result } = renderHook(() => useAutoProjectRuns('ap1'));
    expect(result.current.loading).toBe(true);
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(mocks.listAutoProjectRuns).toHaveBeenCalledWith('ap1', { limit: 20 });
    expect(seqs(result.current.runs)).toEqual([3, 4, 5]);
    expect(result.current.hasOlder).toBe(true);

    mocks.listAutoProjectRuns.mockResolvedValueOnce(page([2, 1]));
    act(() => result.current.loadOlder());
    await waitFor(() => expect(result.current.loadingOlder).toBe(false));
    expect(mocks.listAutoProjectRuns).toHaveBeenLastCalledWith('ap1', { beforeSeq: 3, limit: 20 });
    expect(seqs(result.current.runs)).toEqual([1, 2, 3, 4, 5]);
    expect(result.current.hasOlder).toBe(false);
  });

  it('refetches the newest page on autoproject_changed for its own AutoProject only and merges it', async () => {
    mocks.listAutoProjectRuns.mockResolvedValueOnce(page([5, 4], 4));
    const { result } = renderHook(() => useAutoProjectRuns('ap1'));
    await waitFor(() => expect(result.current.loading).toBe(false));
    mocks.listAutoProjectRuns.mockResolvedValueOnce(page([3, 2], 2));
    act(() => result.current.loadOlder());
    await waitFor(() => expect(seqs(result.current.runs)).toEqual([2, 3, 4, 5]));

    emit('autoproject_changed', { autoproject_id: 'other' });
    emit('autoproject_list_changed');
    expect(mocks.listAutoProjectRuns).toHaveBeenCalledTimes(2);

    // Run 6 started, run 4 was deleted: the page replaces the runs it covers,
    // the older loaded runs (2, 3) stay with their cursor.
    mocks.listAutoProjectRuns.mockResolvedValueOnce({
      runs: [run(6, { status: 'running' }), run(5), run(3)],
      next_before_seq: 3,
    });
    emit('autoproject_changed', { autoproject_id: 'ap1' });
    await waitFor(() => expect(seqs(result.current.runs)).toEqual([2, 3, 5, 6]));
    expect(result.current.runs[3].status).toBe('running');
    expect(result.current.hasOlder).toBe(true);
    expect(mocks.listAutoProjectRuns).toHaveBeenCalledTimes(3);
  });

  it('replaces the list when the fresh page reaches the oldest run', async () => {
    mocks.listAutoProjectRuns.mockResolvedValueOnce(page([3, 2, 1]));
    const { result } = renderHook(() => useAutoProjectRuns('ap1'));
    await waitFor(() => expect(seqs(result.current.runs)).toEqual([1, 2, 3]));

    mocks.listAutoProjectRuns.mockResolvedValueOnce(page([3, 1]));
    emit('autoproject_changed', { autoproject_id: 'ap1' });
    await waitFor(() => expect(seqs(result.current.runs)).toEqual([1, 3]));
  });

  it('replaces the list (with the page cursor) when the fresh page leaves a gap', async () => {
    mocks.listAutoProjectRuns.mockResolvedValueOnce(page([3, 2], 2));
    const { result } = renderHook(() => useAutoProjectRuns('ap1'));
    await waitFor(() => expect(seqs(result.current.runs)).toEqual([2, 3]));

    mocks.listAutoProjectRuns.mockResolvedValueOnce(page([9, 8], 8));
    emit('autoproject_changed', { autoproject_id: 'ap1' });
    await waitFor(() => expect(seqs(result.current.runs)).toEqual([8, 9]));
    expect(result.current.hasOlder).toBe(true);
  });

  it('refetches on a socket reconnect', async () => {
    mocks.listAutoProjectRuns.mockResolvedValueOnce(page([1]));
    const { result } = renderHook(() => useAutoProjectRuns('ap1'));
    await waitFor(() => expect(result.current.loading).toBe(false));

    mocks.listAutoProjectRuns.mockResolvedValueOnce(page([2, 1]));
    reconnect();
    await waitFor(() => expect(seqs(result.current.runs)).toEqual([1, 2]));
  });

  it('skips event and reconnect refetches while this tab deletes the AutoProject', async () => {
    mocks.listAutoProjectRuns.mockResolvedValueOnce(page([1]));
    const { result } = renderHook(() => useAutoProjectRuns('ap1'));
    await waitFor(() => expect(result.current.loading).toBe(false));

    mocks.deleting.add('ap1');
    emit('autoproject_changed', { autoproject_id: 'ap1' });
    reconnect();
    await act(async () => {});
    expect(mocks.listAutoProjectRuns).toHaveBeenCalledTimes(1);
    expect(seqs(result.current.runs)).toEqual([1]);
  });

  it('coalesces a burst of events into one more fetch after the one in flight', async () => {
    mocks.listAutoProjectRuns.mockResolvedValueOnce(page([1]));
    const { result } = renderHook(() => useAutoProjectRuns('ap1'));
    await waitFor(() => expect(result.current.loading).toBe(false));

    let release: (p: AutoProjectRunsPage) => void = () => {};
    mocks.listAutoProjectRuns.mockImplementationOnce(() => new Promise((resolve) => { release = resolve; }));
    mocks.listAutoProjectRuns.mockResolvedValueOnce(page([3, 2, 1]));
    emit('autoproject_changed', { autoproject_id: 'ap1' });
    emit('autoproject_changed', { autoproject_id: 'ap1' });
    emit('autoproject_changed', { autoproject_id: 'ap1' });
    expect(mocks.listAutoProjectRuns).toHaveBeenCalledTimes(2);

    await act(async () => release(page([2, 1])));
    await waitFor(() => expect(seqs(result.current.runs)).toEqual([1, 2, 3]));
    expect(mocks.listAutoProjectRuns).toHaveBeenCalledTimes(3);
  });

  it('reports notFound on a 404 and an error on another failed first load', async () => {
    mocks.listAutoProjectRuns.mockRejectedValueOnce(new ApiClientError('Not found', 404));
    const missing = renderHook(() => useAutoProjectRuns('gone'));
    await waitFor(() => expect(missing.result.current.notFound).toBe(true));
    expect(missing.result.current.error).toBeNull();
    expect(missing.result.current.runs).toEqual([]);

    mocks.listAutoProjectRuns.mockRejectedValueOnce(new ApiClientError('Boom', 500));
    const failed = renderHook(() => useAutoProjectRuns('ap2'));
    await waitFor(() => expect(failed.result.current.error?.message).toBe('Boom'));
    expect(failed.result.current.notFound).toBe(false);
  });

  it('keeps the runs on screen when a background refresh fails', async () => {
    mocks.listAutoProjectRuns.mockResolvedValueOnce(page([2, 1]));
    const { result } = renderHook(() => useAutoProjectRuns('ap1'));
    await waitFor(() => expect(result.current.loading).toBe(false));

    mocks.listAutoProjectRuns.mockRejectedValueOnce(new ApiClientError('Boom', 500));
    await act(async () => { await result.current.refresh(); });
    expect(seqs(result.current.runs)).toEqual([1, 2]);
    expect(result.current.error).toBeNull();
  });

  it('drops a deleted run locally with removeRun', async () => {
    mocks.listAutoProjectRuns.mockResolvedValueOnce(page([2, 1]));
    const { result } = renderHook(() => useAutoProjectRuns('ap1'));
    await waitFor(() => expect(result.current.loading).toBe(false));
    act(() => result.current.removeRun('r1'));
    expect(seqs(result.current.runs)).toEqual([2]);
  });

  it('never shows a previous AutoProject\'s runs after the id changes', async () => {
    let releaseOld: (p: AutoProjectRunsPage) => void = () => {};
    mocks.listAutoProjectRuns.mockImplementation((id: string) =>
      id === 'ap1'
        ? new Promise((resolve) => { releaseOld = resolve; })
        : Promise.resolve(page([7])),
    );
    const { result, rerender } = renderHook(({ id }) => useAutoProjectRuns(id), { initialProps: { id: 'ap1' } });
    rerender({ id: 'ap2' });
    await waitFor(() => expect(seqs(result.current.runs)).toEqual([7]));
    await act(async () => releaseOld(page([1])));
    expect(seqs(result.current.runs)).toEqual([7]);
  });

  it('fetches nothing for a null id', () => {
    const { result } = renderHook(() => useAutoProjectRuns(null));
    expect(result.current.loading).toBe(false);
    expect(mocks.listAutoProjectRuns).not.toHaveBeenCalled();
    expect(mocks.global.size).toBe(0);
  });
});
