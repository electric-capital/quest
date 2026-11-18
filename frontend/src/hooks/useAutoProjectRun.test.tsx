// useAutoProjectRun: the run panel's run, found by its conversation id by
// paging the AutoProject's runs newest first; re-read by seq on every
// `changeSeq` bump and socket reconnect (never while this tab deletes the
// AutoProject). A conversation that is not a run of this AutoProject, a run
// deleted meanwhile and a missing or foreign AutoProject are `notFound`.
import { act, cleanup, renderHook, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiClientError } from '../api/request';
import type { AutoProjectRun, AutoProjectRunsPage } from '../api/types';
import { useAutoProjectRun } from './useAutoProjectRun';

const mocks = vi.hoisted(() => ({
  listAutoProjectRuns: vi.fn(),
  deleting: new Set<string>(),
  reconnect: new Set<() => void>(),
}));

vi.mock('../api/autoprojectsApi', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/autoprojectsApi')>()),
  listAutoProjectRuns: mocks.listAutoProjectRuns,
  isAutoProjectDeleting: (id: string) => mocks.deleting.has(id),
}));

vi.mock('../services/PersistentWebSocket', () => ({
  persistentWebSocket: {
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

/**
 * GET /runs over `total` runs (seq 1..total, minus `deleted`), newest first,
 * honouring before_seq and limit like the server.
 */
function serveRuns(total: number, deleted: number[] = [], status: Record<number, AutoProjectRun['status']> = {}) {
  mocks.listAutoProjectRuns.mockImplementation(
    async (_id: string, { beforeSeq, limit }: { beforeSeq?: number | null; limit: number }): Promise<AutoProjectRunsPage> => {
      const all = Array.from({ length: total }, (_, i) => total - i).filter((s) => !deleted.includes(s));
      const below = all.filter((s) => beforeSeq == null || s < beforeSeq);
      const slice = below.slice(0, limit);
      const more = below.length > slice.length;
      return {
        runs: slice.map((s) => run(s, { status: status[s] ?? 'completed' })),
        next_before_seq: more ? slice[slice.length - 1] : null,
      };
    },
  );
}

beforeEach(() => {
  mocks.listAutoProjectRuns.mockReset();
  mocks.deleting.clear();
  mocks.reconnect.clear();
});

afterEach(cleanup);

describe('useAutoProjectRun', () => {
  it('pages newest first until it finds the run, then re-reads it by seq on each change', async () => {
    serveRuns(250, [], { 30: 'running' });
    const { result, rerender } = renderHook(({ change }) => useAutoProjectRun('ap1', 'c30', change), {
      initialProps: { change: 0 },
    });
    expect(result.current.loading).toBe(true);
    await waitFor(() => expect(result.current.run?.seq).toBe(30));
    expect(result.current.notFound).toBe(false);
    expect(mocks.listAutoProjectRuns.mock.calls.map((c) => c[1])).toEqual([
      { beforeSeq: null, limit: 100 },
      { beforeSeq: 151, limit: 100 },
      { beforeSeq: 51, limit: 100 },
    ]);

    serveRuns(250);
    rerender({ change: 1 });
    await waitFor(() => expect(result.current.run?.status).toBe('completed'));
    expect(mocks.listAutoProjectRuns).toHaveBeenLastCalledWith('ap1', { beforeSeq: 31, limit: 1 });
  });

  it('is notFound for a conversation that is not a run of this AutoProject', async () => {
    serveRuns(3);
    const { result } = renderHook(() => useAutoProjectRun('ap1', 'plain-chat', 0));
    await waitFor(() => expect(result.current.notFound).toBe(true));
    expect(result.current.run).toBeNull();
    expect(result.current.loading).toBe(false);
  });

  it('turns into notFound when a later re-read no longer finds the run (deleted)', async () => {
    serveRuns(3);
    const { result, rerender } = renderHook(({ change }) => useAutoProjectRun('ap1', 'c2', change), {
      initialProps: { change: 0 },
    });
    await waitFor(() => expect(result.current.run?.seq).toBe(2));
    serveRuns(3, [2]);
    rerender({ change: 1 });
    await waitFor(() => expect(result.current.notFound).toBe(true));
  });

  it('is notFound for a missing or foreign AutoProject (404)', async () => {
    mocks.listAutoProjectRuns.mockRejectedValue(new ApiClientError('Not found', 404, 'autoproject_not_found'));
    const { result } = renderHook(() => useAutoProjectRun('foreign', 'c1', 0));
    await waitFor(() => expect(result.current.notFound).toBe(true));
    expect(result.current.error).toBeNull();
  });

  it('re-reads on a socket reconnect but not while this tab deletes the AutoProject', async () => {
    serveRuns(2);
    const { result } = renderHook(() => useAutoProjectRun('ap1', 'c2', 0));
    await waitFor(() => expect(result.current.run?.seq).toBe(2));
    const calls = mocks.listAutoProjectRuns.mock.calls.length;

    act(() => { for (const cb of [...mocks.reconnect]) cb(); });
    await waitFor(() => expect(mocks.listAutoProjectRuns).toHaveBeenCalledTimes(calls + 1));

    mocks.deleting.add('ap1');
    act(() => { for (const cb of [...mocks.reconnect]) cb(); });
    await act(async () => {});
    expect(mocks.listAutoProjectRuns).toHaveBeenCalledTimes(calls + 1);
  });

  it('keeps the run when a background re-read fails, and shows a first-load error', async () => {
    serveRuns(2);
    const { result } = renderHook(() => useAutoProjectRun('ap1', 'c1', 0));
    await waitFor(() => expect(result.current.run?.seq).toBe(1));
    mocks.listAutoProjectRuns.mockRejectedValueOnce(new ApiClientError('Boom', 500));
    await act(async () => { await result.current.refresh(); });
    expect(result.current.run?.seq).toBe(1);
    expect(result.current.error).toBeNull();

    mocks.listAutoProjectRuns.mockRejectedValueOnce(new ApiClientError('Boom', 500));
    const failed = renderHook(() => useAutoProjectRun('ap1', 'c9', 0));
    await waitFor(() => expect(failed.result.current.error?.message).toBe('Boom'));
    expect(failed.result.current.notFound).toBe(false);
  });
});
