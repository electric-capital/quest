import { act, renderHook, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiClientError } from '../api/request';
import type { TableDetail, TableListRow } from '../api/types';
import { useTable } from './useTable';

type GlobalListener = (event: { type: string; [key: string]: unknown }) => void;

const mocks = vi.hoisted(() => ({
  getTable: vi.fn<(id: string) => Promise<TableDetail>>(),
  global: new Set<(event: { type: string; [key: string]: unknown }) => void>(),
}));

vi.mock('../api/tablesApi', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/tablesApi')>()),
  getTable: mocks.getTable,
}));

vi.mock('../services/PersistentWebSocket', () => ({
  persistentWebSocket: {
    onGlobalEvent: (cb: GlobalListener) => {
      mocks.global.add(cb);
      return () => mocks.global.delete(cb);
    },
  },
}));

const T0 = '2026-10-01T00:00:00';
const T1 = '2026-10-01T00:00:05';

function row(id: string, overrides: Partial<TableListRow> = {}): TableListRow {
  return {
    id,
    owner_id: 1,
    project_id: null,
    title: `Table ${id}`,
    description: '',
    mode: 'private',
    schema_version: 1,
    row_count: 3,
    last_write_source: 'ui:1',
    require_approval: false,
    created_at: T0,
    updated_at: T0,
    scope: 'user',
    column_count: 1,
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

function detail(id: string, overrides: Partial<TableDetail> = {}): TableDetail {
  return {
    ...row(id),
    schema: [{
      key: 'c_0000000a', name: 'Name', type: 'text', required: false, unique: false,
      default: null, options: null, multiple: false, precision: null, description: '',
    }],
    last_write_conversation: null,
    ...overrides,
  };
}

function emit(event: { type: string; [key: string]: unknown }) {
  for (const cb of mocks.global) cb(event);
}

function changed(tableId: string, updatedAt: string | null, schemaVersion: number | null) {
  emit({ type: 'table_changed', table_id: tableId, updated_at: updatedAt, schema_version: schemaVersion });
}

const notFoundError = () => new ApiClientError('x', 404, 'table_not_found');

async function loaded(id = 't1', first: TableDetail = detail(id)) {
  mocks.getTable.mockResolvedValueOnce(first);
  const hook = renderHook(() => useTable(id));
  await waitFor(() => expect(hook.result.current.loading).toBe(false));
  return hook;
}

describe('useTable', () => {
  beforeEach(() => {
    mocks.getTable.mockReset();
    mocks.global.clear();
  });

  it('loads the table', async () => {
    const { result } = await loaded();
    expect(mocks.getTable).toHaveBeenCalledWith('t1');
    expect(result.current.table?.schema[0].key).toBe('c_0000000a');
    expect(result.current.notFound).toBe(false);
    expect(result.current.error).toBeNull();
    expect(result.current.changeSeq).toBe(0);
    expect(result.current.lastChange).toBeNull();
  });

  it('does nothing for a null id', async () => {
    const { result } = renderHook(() => useTable(null));
    expect(result.current.loading).toBe(false);
    await act(async () => {
      await result.current.refresh();
    });
    expect(mocks.getTable).not.toHaveBeenCalled();
    expect(mocks.global.size).toBe(0);
  });

  it('reports notFound on a 404 and other failures as an ApiClientError', async () => {
    mocks.getTable.mockRejectedValueOnce(notFoundError());
    const missing = renderHook(() => useTable('missing'));
    await waitFor(() => expect(missing.result.current.loading).toBe(false));
    expect(missing.result.current.notFound).toBe(true);
    expect(missing.result.current.table).toBeNull();

    mocks.getTable.mockRejectedValueOnce(new ApiClientError('off', 403, 'tables_disabled'));
    const off = renderHook(() => useTable('t2'));
    await waitFor(() => expect(off.result.current.loading).toBe(false));
    expect(off.result.current.notFound).toBe(false);
    expect(off.result.current.error?.errorCode).toBe('tables_disabled');
  });

  it('ignores table_changed for other tables and unrelated events', async () => {
    const { result } = await loaded();
    act(() => {
      changed('t2', T1, 2);
      changed('t2', null, null);
      emit({ type: 'table_list_changed' });
      emit({ type: 'doc_changed', doc_id: 't1', updated_at: null });
    });
    expect(mocks.getTable).toHaveBeenCalledTimes(1);
    expect(result.current.changeSeq).toBe(0);
    expect(result.current.lastChange).toBeNull();
  });

  it('re-fetches when updated_at moves and counts every change', async () => {
    const { result } = await loaded();
    mocks.getTable.mockResolvedValueOnce(detail('t1', { updated_at: T1, row_count: 4 }));
    act(() => changed('t1', T1, 1));
    expect(result.current.changeSeq).toBe(1);
    expect(result.current.lastChange).toEqual({ table_id: 't1', updated_at: T1, schema_version: 1 });
    await waitFor(() => expect(result.current.table?.row_count).toBe(4));
    expect(mocks.getTable).toHaveBeenCalledTimes(2);

    // Already showing this updated_at and schema_version: no detail re-fetch,
    // but the change still counts (a rows hook re-queries on it).
    act(() => changed('t1', T1, 1));
    expect(mocks.getTable).toHaveBeenCalledTimes(2);
    expect(result.current.changeSeq).toBe(2);
  });

  it('re-fetches on a schema_version bump', async () => {
    const { result } = await loaded();
    mocks.getTable.mockResolvedValueOnce(detail('t1', { schema_version: 2 }));
    // Same updated_at as shown, newer schema: still re-fetched.
    act(() => changed('t1', T0, 2));
    await waitFor(() => expect(result.current.table?.schema_version).toBe(2));
    expect(mocks.getTable).toHaveBeenCalledTimes(2);
  });

  it('turns a null updated_at into notFound through the follow-up 404', async () => {
    const { result } = await loaded();
    mocks.getTable.mockRejectedValueOnce(notFoundError());
    act(() => changed('t1', null, null));
    expect(result.current.lastChange).toEqual({ table_id: 't1', updated_at: null, schema_version: null });
    await waitFor(() => expect(result.current.notFound).toBe(true));
    expect(result.current.table).toBeNull();
    expect(mocks.getTable).toHaveBeenCalledTimes(2);
  });

  it('always re-fetches on a null updated_at, even when access only changed', async () => {
    const { result } = await loaded();
    mocks.getTable.mockResolvedValueOnce(detail('t1', { shared: true }));
    act(() => changed('t1', null, null));
    await waitFor(() => expect(result.current.table?.shared).toBe(true));
    expect(result.current.notFound).toBe(false);
  });

  it('keeps the loaded table when a background refresh fails', async () => {
    const { result } = await loaded();
    mocks.getTable.mockRejectedValueOnce(new ApiClientError('busy', 503, 'busy'));
    await act(async () => {
      await result.current.refresh();
    });
    expect(result.current.table?.id).toBe('t1');
    expect(result.current.error).toBeNull();
  });

  it('resets on an id change and drops a late response for the old id', async () => {
    let resolveOld!: (value: TableDetail) => void;
    mocks.getTable.mockReturnValueOnce(new Promise((res) => { resolveOld = res; }));
    const { result, rerender } = renderHook(({ id }: { id: string }) => useTable(id), {
      initialProps: { id: 't1' },
    });
    mocks.getTable.mockResolvedValueOnce(detail('t2'));
    rerender({ id: 't2' });
    await waitFor(() => expect(result.current.table?.id).toBe('t2'));
    await act(async () => {
      resolveOld(detail('t1'));
    });
    expect(result.current.table?.id).toBe('t2');

    // Events for the old id no longer count.
    act(() => changed('t1', T1, 1));
    expect(result.current.changeSeq).toBe(0);
  });

  it('applies a newer detail and ignores an older one', async () => {
    const { result } = await loaded('t1', detail('t1', { updated_at: T1 }));
    act(() => result.current.applyDetail(detail('t1', { updated_at: T0, title: 'Old' })));
    expect(result.current.table?.title).toBe('Table t1');
    act(() =>
      result.current.applyDetail({ ...detail('t1', { updated_at: T1, title: 'Renamed' }), last_write_conversation: undefined }),
    );
    expect(result.current.table?.title).toBe('Renamed');
    expect(mocks.getTable).toHaveBeenCalledTimes(1);
  });

  it('merges a list row but re-fetches when it names another schema_version', async () => {
    const { result } = await loaded();
    act(() => result.current.applyRow(row('t1', { shared: true })));
    expect(result.current.table?.shared).toBe(true);
    expect(result.current.table?.schema).toHaveLength(1);
    expect(mocks.getTable).toHaveBeenCalledTimes(1);

    mocks.getTable.mockResolvedValueOnce(detail('t1', { schema_version: 3, updated_at: T1 }));
    act(() => result.current.applyRow(row('t1', { schema_version: 3, updated_at: T1 })));
    // Not merged onto the old schema; the re-fetch brings both.
    expect(result.current.table?.schema_version).toBe(1);
    await waitFor(() => expect(result.current.table?.schema_version).toBe(3));
  });

  it('coalesces a burst of changes into one follow-up fetch instead of dropping the in-flight one', async () => {
    const { result } = await loaded();
    const T2 = '2026-10-01T00:00:09';
    let resolveFirst: (d: TableDetail) => void = () => {};
    mocks.getTable.mockImplementationOnce(() => new Promise((r) => { resolveFirst = r; }));
    mocks.getTable.mockResolvedValueOnce(detail('t1', { updated_at: T2, row_count: 9 }));
    act(() => changed('t1', T1, 1));
    act(() => changed('t1', '2026-10-01T00:00:07', 1));
    act(() => changed('t1', T2, 1));
    // One fetch in flight; the later events only mark it dirty.
    expect(mocks.getTable).toHaveBeenCalledTimes(2);
    await act(async () => resolveFirst(detail('t1', { updated_at: T1, row_count: 5 })));
    // The in-flight response lands rather than being discarded...
    await waitFor(() => expect(result.current.table?.row_count).toBeGreaterThanOrEqual(5));
    // ...and exactly one more fetch brings the latest state.
    await waitFor(() => expect(result.current.table?.row_count).toBe(9));
    expect(mocks.getTable).toHaveBeenCalledTimes(3);
    expect(result.current.changeSeq).toBe(3);
  });

  it('strips change_id from a schema-edit response', async () => {
    const { result } = await loaded();
    act(() => result.current.applyDetail({ ...detail('t1', { updated_at: T1 }), change_id: 7 } as TableDetail));
    expect(result.current.table && 'change_id' in result.current.table).toBe(false);
  });
});
