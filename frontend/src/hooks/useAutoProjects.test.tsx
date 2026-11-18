// useAutoProjects: the sidebar's (or the archive's) AutoProject list. Loads on
// mount, re-fetches once after a burst of `autoproject_list_changed` or a
// socket reconnect, keeps the list on a failed refresh, merges each row's
// unread seqs into the store, and does nothing with the gate closed.
import { act, cleanup, renderHook, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiClientError } from '../api/request';
import type { AutoProjectListRow, ListAutoProjectsResponse } from '../api/types';
import { unreadStore } from '../store/unreadStore';
import { AUTOPROJECTS_REFRESH_DEBOUNCE_MS, useAutoProjects } from './useAutoProjects';

type GlobalListener = (event: { type: string; [key: string]: unknown }) => void;

const mocks = vi.hoisted(() => ({
  listAutoProjects: vi.fn(),
  markAutoProjectRead: vi.fn(),
  global: new Set<(event: { type: string; [key: string]: unknown }) => void>(),
  reconnect: new Set<() => void>(),
}));

vi.mock('../api/autoprojectsApi', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/autoprojectsApi')>()),
  listAutoProjects: mocks.listAutoProjects,
  markAutoProjectRead: mocks.markAutoProjectRead,
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

function row(id: string, overrides: Partial<AutoProjectListRow> = {}): AutoProjectListRow {
  return {
    id,
    title: `AutoProject ${id}`,
    archived: false,
    last_activity_at: '2026-11-10T10:00:00',
    active_run: null,
    unread: false,
    event_seq: 1,
    unread_seq: 0,
    read_seq: 0,
    ...overrides,
  };
}

function list(...rows: AutoProjectListRow[]): ListAutoProjectsResponse {
  return { autoprojects: rows };
}

function emit(type: string) {
  act(() => {
    for (const cb of [...mocks.global]) cb({ type });
  });
}

const ids = (rows: AutoProjectListRow[]) => rows.map((r) => r.id);

beforeEach(() => {
  mocks.listAutoProjects.mockReset();
  mocks.global.clear();
  mocks.reconnect.clear();
  unreadStore.reset();
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe('useAutoProjects', () => {
  it.each([
    [{}, { archived: false }],
    [{ archived: true }, { archived: true }],
  ])('loads the list for %o in server order', async (opts, call) => {
    mocks.listAutoProjects.mockResolvedValueOnce(list(row('b'), row('a')));
    const { result } = renderHook(() => useAutoProjects(opts));
    expect(result.current.loading).toBe(true);
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(mocks.listAutoProjects).toHaveBeenCalledWith(call);
    expect(ids(result.current.autoprojects)).toEqual(['b', 'a']);
  });

  it('merges row unread seqs into the unread store', async () => {
    mocks.listAutoProjects.mockResolvedValueOnce(list(row('a', { unread_seq: 4, read_seq: 1 }), row('b')));
    const { result } = renderHook(() => useAutoProjects());
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(unreadStore.isAutoProjectUnread('a')).toBe(true);
    expect(unreadStore.isAutoProjectUnread('b')).toBe(false);
  });

  it('re-fetches once after a burst of autoproject_list_changed and on reconnect', async () => {
    mocks.listAutoProjects.mockResolvedValue(list(row('a')));
    const { result } = renderHook(() => useAutoProjects());
    await waitFor(() => expect(result.current.loading).toBe(false));
    vi.useFakeTimers();

    emit('autoproject_list_changed');
    emit('autoproject_changed');
    emit('autoproject_list_changed');
    await act(async () => { await vi.advanceTimersByTimeAsync(AUTOPROJECTS_REFRESH_DEBOUNCE_MS - 1); });
    expect(mocks.listAutoProjects).toHaveBeenCalledTimes(1);
    mocks.listAutoProjects.mockResolvedValue(list(row('c'), row('a')));
    await act(async () => { await vi.advanceTimersByTimeAsync(1); });
    expect(mocks.listAutoProjects).toHaveBeenCalledTimes(2);
    expect(ids(result.current.autoprojects)).toEqual(['c', 'a']);
    // A silent refresh never shows the loading state.
    expect(result.current.loading).toBe(false);

    act(() => { for (const cb of [...mocks.reconnect]) cb(); });
    await act(async () => { await vi.advanceTimersByTimeAsync(AUTOPROJECTS_REFRESH_DEBOUNCE_MS); });
    expect(mocks.listAutoProjects).toHaveBeenCalledTimes(3);
  });

  it('shows a failed first load and keeps the list when a refresh fails', async () => {
    mocks.listAutoProjects.mockRejectedValueOnce(new ApiClientError('Forbidden', 403, 'autoprojects_disabled'));
    const failed = renderHook(() => useAutoProjects());
    await waitFor(() => expect(failed.result.current.error).toBe('Forbidden'));
    expect(failed.result.current.autoprojects).toEqual([]);
    failed.unmount();

    mocks.listAutoProjects.mockResolvedValueOnce(list(row('a')));
    const { result } = renderHook(() => useAutoProjects());
    await waitFor(() => expect(result.current.loading).toBe(false));
    mocks.listAutoProjects.mockRejectedValueOnce(new ApiClientError('Boom', 500));
    await act(async () => { await result.current.refresh(); });
    expect(ids(result.current.autoprojects)).toEqual(['a']);
    expect(result.current.error).toBeNull();
  });

  it('fetches nothing and ignores events with the gate closed', async () => {
    vi.useFakeTimers();
    const { result } = renderHook(() => useAutoProjects({ enabled: false }));
    expect(result.current.loading).toBe(false);
    expect(result.current.autoprojects).toEqual([]);
    expect(mocks.global.size).toBe(0);
    expect(mocks.reconnect.size).toBe(0);
    await act(async () => { await vi.advanceTimersByTimeAsync(AUTOPROJECTS_REFRESH_DEBOUNCE_MS); });
    expect(mocks.listAutoProjects).not.toHaveBeenCalled();
  });

  it('drops a superseded response when switching to the archive', async () => {
    let releaseActive: (r: ListAutoProjectsResponse) => void = () => {};
    mocks.listAutoProjects.mockImplementation(({ archived }: { archived: boolean }) =>
      archived
        ? Promise.resolve(list(row('old', { archived: true })))
        : new Promise((resolve) => { releaseActive = resolve; }),
    );
    const { result, rerender } = renderHook(({ archived }) => useAutoProjects({ archived }), {
      initialProps: { archived: false },
    });
    rerender({ archived: true });
    await waitFor(() => expect(ids(result.current.autoprojects)).toEqual(['old']));
    await act(async () => releaseActive(list(row('a'))));
    expect(ids(result.current.autoprojects)).toEqual(['old']);
  });
});
