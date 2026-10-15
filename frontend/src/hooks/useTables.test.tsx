import { act, renderHook, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { ListTablesOptions } from '../api/tablesApi';
import { ApiClientError } from '../api/request';
import type { ListTablesResponse, Project, TableListRow } from '../api/types';
import { TABLES_REFRESH_DEBOUNCE_MS, useTables } from './useTables';
import { TABLES_INDEX_REFRESH_DEBOUNCE_MS, useProjectTablesIndex } from './useProjectTablesIndex';

type GlobalListener = (event: { type: string; [key: string]: unknown }) => void;

const mocks = vi.hoisted(() => ({
  listTables: vi.fn<(opts: ListTablesOptions) => Promise<ListTablesResponse>>(),
  global: new Set<(event: { type: string; [key: string]: unknown }) => void>(),
}));

vi.mock('../api/tablesApi', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/tablesApi')>()),
  listTables: mocks.listTables,
}));

vi.mock('../services/PersistentWebSocket', () => ({
  persistentWebSocket: {
    onGlobalEvent: (cb: GlobalListener) => {
      mocks.global.add(cb);
      return () => mocks.global.delete(cb);
    },
  },
}));

function tableRow(id: string, overrides: Partial<TableListRow> = {}): TableListRow {
  return {
    id,
    owner_id: 1,
    project_id: null,
    title: `Table ${id}`,
    description: '',
    mode: 'private',
    schema_version: 1,
    row_count: 0,
    last_write_source: 'ui:1',
    require_approval: false,
    created_at: '2026-10-01T00:00:00',
    updated_at: '2026-10-01T00:00:00',
    scope: 'user',
    column_count: 2,
    shared: false,
    shared_with_me: false,
    permission: null,
    owner: null,
    shares: [],
    last_write_user: null,
    access: {
      can_read: true, rows: 'free', schema: 'approval', can_edit: true, can_restore: true,
      can_rename: true, can_delete: true, can_share: true, can_edit_schema: true,
      can_require_approval: true, can_download_history: true, deny_reason: null,
      schema_deny_reason: null,
    },
    ...overrides,
  };
}

function page(ids: string[], nextCursor: string | null = null): ListTablesResponse {
  return { tables: ids.map((id) => tableRow(id)), has_more: nextCursor !== null, next_cursor: nextCursor };
}

function emit(type: string, extra: Record<string, unknown> = {}) {
  for (const cb of mocks.global) cb({ type, ...extra });
}

function project(id: string): Project {
  return { id, name: id } as Project;
}

describe('useTables', () => {
  beforeEach(() => {
    mocks.listTables.mockReset();
    mocks.global.clear();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('loads the first page and appends the next one by cursor', async () => {
    mocks.listTables.mockResolvedValueOnce(page(['a', 'b'], 'cursor-1'));
    const { result } = renderHook(() => useTables({ limit: 2 }));

    expect(result.current.loading).toBe(true);
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(mocks.listTables).toHaveBeenCalledWith({ projectId: null, limit: 2 });
    expect(result.current.tables.map((t) => t.id)).toEqual(['a', 'b']);
    expect(result.current.hasMore).toBe(true);

    mocks.listTables.mockResolvedValueOnce(page(['b', 'c']));
    act(() => result.current.loadMore());
    await waitFor(() => expect(result.current.loadingMore).toBe(false));
    expect(mocks.listTables).toHaveBeenLastCalledWith({ projectId: null, limit: 2, cursor: 'cursor-1' });
    // The overlapping row is deduped.
    expect(result.current.tables.map((t) => t.id)).toEqual(['a', 'b', 'c']);
    expect(result.current.hasMore).toBe(false);
  });

  it('passes the project id through, and the shared stream ignores it', async () => {
    mocks.listTables.mockResolvedValue(page(['x']));
    const project = renderHook(() => useTables({ projectId: 'proj-1' }));
    await waitFor(() => expect(project.result.current.loading).toBe(false));
    expect(mocks.listTables).toHaveBeenLastCalledWith({ projectId: 'proj-1', limit: 50 });

    const shared = renderHook(() => useTables({ shared: true, projectId: 'proj-1', limit: 5 }));
    await waitFor(() => expect(shared.result.current.loading).toBe(false));
    expect(mocks.listTables).toHaveBeenLastCalledWith({ shared: true, limit: 5 });
  });

  it('fetches nothing and ignores events while the gate is closed', async () => {
    vi.useFakeTimers();
    const { result } = renderHook(() => useTables({ enabled: false }));
    expect(result.current.loading).toBe(false);
    expect(result.current.tables).toEqual([]);
    expect(mocks.global.size).toBe(0);
    await act(async () => {
      emit('table_list_changed');
      await vi.advanceTimersByTimeAsync(TABLES_REFRESH_DEBOUNCE_MS * 2);
      await result.current.refresh();
    });
    expect(mocks.listTables).not.toHaveBeenCalled();
  });

  it('starts fetching when the gate opens', async () => {
    mocks.listTables.mockResolvedValue(page(['a']));
    const { result, rerender } = renderHook(
      ({ enabled }: { enabled: boolean }) => useTables({ enabled }),
      { initialProps: { enabled: false } },
    );
    expect(mocks.listTables).not.toHaveBeenCalled();
    rerender({ enabled: true });
    await waitFor(() => expect(result.current.tables.map((t) => t.id)).toEqual(['a']));
    expect(mocks.listTables).toHaveBeenCalledTimes(1);
  });

  it('coalesces a burst of table_list_changed into one refresh after the quiet period', async () => {
    vi.useFakeTimers();
    mocks.listTables.mockResolvedValueOnce(page(['a']));
    const { result } = renderHook(() => useTables({ limit: 5 }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(result.current.tables.map((t) => t.id)).toEqual(['a']);
    expect(mocks.listTables).toHaveBeenCalledTimes(1);

    mocks.listTables.mockResolvedValueOnce(page(['b', 'a']));
    await act(async () => {
      emit('table_list_changed');
      await vi.advanceTimersByTimeAsync(TABLES_REFRESH_DEBOUNCE_MS - 50);
      emit('table_list_changed');
      await vi.advanceTimersByTimeAsync(TABLES_REFRESH_DEBOUNCE_MS - 50);
      emit('table_list_changed');
    });
    // Each event restarted the timer: nothing fetched yet.
    expect(mocks.listTables).toHaveBeenCalledTimes(1);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(TABLES_REFRESH_DEBOUNCE_MS);
    });
    expect(mocks.listTables).toHaveBeenCalledTimes(2);
    expect(mocks.listTables).toHaveBeenLastCalledWith({ projectId: null, limit: 5 });
    expect(result.current.tables.map((t) => t.id)).toEqual(['b', 'a']);
  });

  it('ignores unrelated events', async () => {
    vi.useFakeTimers();
    mocks.listTables.mockResolvedValueOnce(page(['a']));
    renderHook(() => useTables());
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    await act(async () => {
      emit('doc_list_changed');
      emit('table_changed', { table_id: 'a', updated_at: '2026-10-02T00:00:00', schema_version: 1 });
      emit('conversation_list_changed');
      await vi.advanceTimersByTimeAsync(TABLES_REFRESH_DEBOUNCE_MS * 2);
    });
    expect(mocks.listTables).toHaveBeenCalledTimes(1);
  });

  it('cancels a pending refresh on unmount', async () => {
    vi.useFakeTimers();
    mocks.listTables.mockResolvedValueOnce(page(['a']));
    const { unmount } = renderHook(() => useTables());
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    act(() => emit('table_list_changed'));
    unmount();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(TABLES_REFRESH_DEBOUNCE_MS * 2);
    });
    expect(mocks.listTables).toHaveBeenCalledTimes(1);
  });

  it('shows a first-load error but keeps the list when a refresh fails', async () => {
    mocks.listTables.mockRejectedValueOnce(new ApiClientError('Quest Tables is off', 403, 'tables_disabled'));
    const failed = renderHook(() => useTables({ projectId: 'p-err' }));
    await waitFor(() => expect(failed.result.current.loading).toBe(false));
    expect(failed.result.current.error).toBe('Quest Tables is off');

    mocks.listTables.mockResolvedValueOnce(page(['a']));
    const { result } = renderHook(() => useTables());
    await waitFor(() => expect(result.current.tables).toHaveLength(1));
    mocks.listTables.mockRejectedValueOnce(new Error('network'));
    await act(async () => {
      await result.current.refresh();
    });
    expect(result.current.tables.map((t) => t.id)).toEqual(['a']);
    expect(result.current.error).toBeNull();
  });
});

describe('useProjectTablesIndex', () => {
  beforeEach(() => {
    mocks.listTables.mockReset();
    mocks.global.clear();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('fans out one list per project and reports failed projects', async () => {
    mocks.listTables.mockImplementation(async (opts) => {
      if (opts.projectId === 'p2') throw new ApiClientError('nope', 404, 'project_not_found');
      return page([`${opts.projectId}-t`]);
    });
    const { result } = renderHook(() => useProjectTablesIndex([project('p1'), project('p2')], true));
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(mocks.listTables).toHaveBeenCalledWith({ projectId: 'p1', limit: 200 });
    expect(Object.keys(result.current.byProject)).toEqual(['p1']);
    expect(result.current.byProject.p1.map((t) => t.id)).toEqual(['p1-t']);
    expect(result.current.failedProjectIds).toEqual(['p2']);
  });

  it('does nothing while disabled', () => {
    const { result } = renderHook(() => useProjectTablesIndex([project('p1')], false));
    expect(result.current).toEqual({ byProject: {}, loading: false, loaded: false, failedProjectIds: [] });
    expect(mocks.listTables).not.toHaveBeenCalled();
    expect(mocks.global.size).toBe(0);
  });

  it('re-fans-out once per burst of table_list_changed only', async () => {
    vi.useFakeTimers();
    mocks.listTables.mockImplementation(async (opts) => page([`${opts.projectId}-t`]));
    renderHook(() => useProjectTablesIndex([project('p1')], true));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(mocks.listTables).toHaveBeenCalledTimes(1);
    await act(async () => {
      emit('doc_list_changed');
      emit('table_list_changed');
      emit('table_list_changed');
      await vi.advanceTimersByTimeAsync(TABLES_INDEX_REFRESH_DEBOUNCE_MS);
    });
    expect(mocks.listTables).toHaveBeenCalledTimes(2);
  });
});
