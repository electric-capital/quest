// useAutoProject: one AutoProject's detail, re-fetched on `autoproject_changed`
// for this AutoProject unless the shown detail already matches the event, on a
// debounced `autoproject_list_changed` and on a socket reconnect; never for an
// AutoProject this tab is deleting. A 404 (missing, foreign or deleted) is
// `notFound`; PATCH and brief responses merge without a refetch.
import { act, cleanup, renderHook, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiClientError } from '../api/request';
import type { AutoProject } from '../api/types';
import { unreadStore } from '../store/unreadStore';
import { useAutoProject } from './useAutoProject';
import { AUTOPROJECTS_REFRESH_DEBOUNCE_MS } from './useAutoProjects';

type GlobalListener = (event: { type: string; [key: string]: unknown }) => void;

const mocks = vi.hoisted(() => ({
  getAutoProject: vi.fn(),
  markAutoProjectRead: vi.fn(),
  deleting: new Set<string>(),
  global: new Set<(event: { type: string; [key: string]: unknown }) => void>(),
  reconnect: new Set<() => void>(),
}));

vi.mock('../api/autoprojectsApi', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/autoprojectsApi')>()),
  getAutoProject: mocks.getAutoProject,
  markAutoProjectRead: mocks.markAutoProjectRead,
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

function detail(overrides: Partial<AutoProject> = {}): AutoProject {
  return {
    id: 'ap1',
    title: 'Quarterly report',
    title_is_default: false,
    archived: false,
    brief: 'Brief v1',
    brief_version: 1,
    brief_updated_at: '2026-11-10T10:00:00',
    brief_source: 'ui',
    brief_run_seq: null,
    model: null,
    thinking_effort: null,
    run_count: 1,
    active_run: null,
    event_seq: 3,
    unread_seq: 0,
    read_seq: 0,
    unread: false,
    created_at: '2026-11-10T09:00:00',
    updated_at: '2026-11-10T10:00:00',
    last_activity_at: '2026-11-10T10:00:00',
    ...overrides,
  };
}

function emit(type: string, extra: Record<string, unknown> = {}) {
  act(() => {
    for (const cb of [...mocks.global]) cb({ type, ...extra });
  });
}

const CHANGED = { autoproject_id: 'ap1', event_seq: 3, brief_version: 1, active_run: null };

async function loaded(id: string | null = 'ap1') {
  const hook = renderHook(({ apId }) => useAutoProject(apId), { initialProps: { apId: id } });
  await waitFor(() => expect(hook.result.current.loading).toBe(false));
  return hook;
}

beforeEach(() => {
  mocks.getAutoProject.mockReset().mockResolvedValue(detail());
  mocks.markAutoProjectRead.mockReset().mockResolvedValue({ unread_seq: 0, read_seq: 0 });
  mocks.deleting.clear();
  mocks.global.clear();
  mocks.reconnect.clear();
  unreadStore.reset();
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe('useAutoProject', () => {
  it('loads the detail and merges its unread seqs into the store', async () => {
    mocks.getAutoProject.mockResolvedValueOnce(detail({ unread_seq: 5, read_seq: 2 }));
    const { result } = await loaded();
    expect(mocks.getAutoProject).toHaveBeenCalledWith('ap1');
    expect(result.current.autoproject?.title).toBe('Quarterly report');
    expect(result.current.changeSeq).toBe(0);
    expect(unreadStore.isAutoProjectUnread('ap1')).toBe(true);
  });

  it('counts autoproject_changed for its own AutoProject and refetches only when the event differs', async () => {
    const { result } = await loaded();

    emit('autoproject_changed', { ...CHANGED, autoproject_id: 'other', event_seq: 9 });
    expect(result.current.changeSeq).toBe(0);

    // Matches what is shown: counted, no refetch.
    emit('autoproject_changed', CHANGED);
    expect(result.current.changeSeq).toBe(1);
    expect(mocks.getAutoProject).toHaveBeenCalledTimes(1);

    const differing: Record<string, unknown>[] = [
      { event_seq: 4 },
      { brief_version: 2 },
      { active_run: { id: 'r2', seq: 2, status: 'running' } },
    ];
    for (const [i, change] of differing.entries()) {
      emit('autoproject_changed', { ...CHANGED, ...change });
      await waitFor(() => expect(mocks.getAutoProject).toHaveBeenCalledTimes(i + 2));
    }
    expect(result.current.changeSeq).toBe(4);
  });

  it('refetches after a debounced autoproject_list_changed burst', async () => {
    await loaded();
    vi.useFakeTimers();
    emit('autoproject_list_changed');
    emit('autoproject_list_changed');
    await act(async () => { await vi.advanceTimersByTimeAsync(AUTOPROJECTS_REFRESH_DEBOUNCE_MS - 1); });
    expect(mocks.getAutoProject).toHaveBeenCalledTimes(1);
    await act(async () => { await vi.advanceTimersByTimeAsync(1); });
    expect(mocks.getAutoProject).toHaveBeenCalledTimes(2);
  });

  it('refetches on a socket reconnect', async () => {
    await loaded();
    act(() => { for (const cb of [...mocks.reconnect]) cb(); });
    await waitFor(() => expect(mocks.getAutoProject).toHaveBeenCalledTimes(2));
  });

  it('never refetches an AutoProject this tab is deleting', async () => {
    const { result } = await loaded();
    mocks.deleting.add('ap1');
    vi.useFakeTimers();
    emit('autoproject_changed', { ...CHANGED, event_seq: 4, active_run: null });
    emit('autoproject_list_changed');
    act(() => { for (const cb of [...mocks.reconnect]) cb(); });
    await act(async () => { await vi.advanceTimersByTimeAsync(AUTOPROJECTS_REFRESH_DEBOUNCE_MS); });
    expect(mocks.getAutoProject).toHaveBeenCalledTimes(1);
    expect(result.current.notFound).toBe(false);
  });

  it('turns a 404 refetch (deleted elsewhere) into notFound', async () => {
    const { result } = await loaded();
    mocks.getAutoProject.mockRejectedValueOnce(new ApiClientError('Not found', 404, 'autoproject_not_found'));
    emit('autoproject_changed', { ...CHANGED, event_seq: 4 });
    await waitFor(() => expect(result.current.notFound).toBe(true));
    expect(result.current.autoproject).toBeNull();
    expect(result.current.error).toBeNull();
  });

  it('shows an error for a failed first load and keeps the detail when a refresh fails', async () => {
    mocks.getAutoProject.mockRejectedValueOnce(new ApiClientError('Boom', 500));
    const first = renderHook(() => useAutoProject('ap1'));
    await waitFor(() => expect(first.result.current.error?.message).toBe('Boom'));
    first.unmount();

    const { result } = await loaded();
    mocks.getAutoProject.mockRejectedValueOnce(new ApiClientError('Boom', 500));
    await act(async () => { await result.current.refresh(); });
    expect(result.current.autoproject?.id).toBe('ap1');
    expect(result.current.error).toBeNull();
  });

  it('merges PATCH responses and brief writes, ignoring older ones and other ids', async () => {
    const { result } = await loaded();

    act(() => result.current.applyDetail(detail({ title: 'Renamed', updated_at: '2026-11-10T11:00:00' })));
    expect(result.current.autoproject?.title).toBe('Renamed');
    act(() => result.current.applyDetail(detail({ title: 'Stale', updated_at: '2026-11-10T09:30:00' })));
    act(() => result.current.applyDetail(detail({ id: 'other', title: 'Other', updated_at: '2026-11-10T12:00:00' })));
    expect(result.current.autoproject?.title).toBe('Renamed');

    const brief = { brief_updated_at: null, brief_source: 'ui', brief_run_seq: null };
    act(() => result.current.applyBrief({ ...brief, brief: 'Brief v2', brief_version: 2 }));
    expect(result.current.autoproject?.brief).toBe('Brief v2');
    act(() => result.current.applyBrief({ ...brief, brief: 'Brief v0', brief_version: 0 }));
    expect(result.current.autoproject?.brief).toBe('Brief v2');
    expect(result.current.autoproject?.title).toBe('Renamed');
    expect(mocks.getAutoProject).toHaveBeenCalledTimes(1);
  });

  it('resets the detail and change count when the id changes', async () => {
    const hook = await loaded();
    emit('autoproject_changed', CHANGED);
    expect(hook.result.current.changeSeq).toBe(1);

    mocks.getAutoProject.mockResolvedValueOnce(detail({ id: 'ap2', title: 'Second' }));
    hook.rerender({ apId: 'ap2' });
    expect(hook.result.current.autoproject).toBeNull();
    expect(hook.result.current.changeSeq).toBe(0);
    await waitFor(() => expect(hook.result.current.autoproject?.title).toBe('Second'));
  });

  it('fetches nothing for a null id', () => {
    const { result } = renderHook(() => useAutoProject(null));
    expect(result.current.loading).toBe(false);
    expect(mocks.getAutoProject).not.toHaveBeenCalled();
    expect(mocks.global.size).toBe(0);
  });
});
