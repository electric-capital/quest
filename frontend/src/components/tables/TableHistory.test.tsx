// TableHistory: list rendering states (in effect, rewound, restore entry,
// conversation link, kind badges, Load more, zoned timestamps, Retry), the
// rows-detail mapping (changeDetailToRowsDiff), restore impact / confirm
// text, the detail cache, the Restore flow (focus after it, Escape in the
// confirm) and its errors, and the lost-access state (403 and 404
// table_not_found). The two shared diff previews are mocked: these tests
// check what the drawer hands them.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { ApiClientError } from '../../api/request';
import type {
  ListTableChangesResponse,
  RestoreTableResponse,
  TableChange,
  TableChangeColumn,
  TableChangeDetail,
  TableDetail,
  TableRowsDiff,
  TableSchemaDiff,
} from '../../api/types';
import {
  REMOVED_COLUMN_LABEL,
  TableHistory,
  actorLabel,
  changeDetailToRowsDiff,
  columnLabels,
  createdColumns,
  droppedValueCounts,
  isCurrentState,
  restoreConfirmText,
  restoreImpact,
} from './TableHistory';

type GlobalListener = (event: { type: string; [key: string]: unknown }) => void;

const mocks = vi.hoisted(() => ({
  listTableChanges: vi.fn(),
  getTableChange: vi.fn(),
  restoreTable: vi.fn(),
  global: new Set<(event: { type: string; [key: string]: unknown }) => void>(),
}));

vi.mock('../../api/tablesApi', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../api/tablesApi')>()),
  listTableChanges: mocks.listTableChanges,
  getTableChange: mocks.getTableChange,
  restoreTable: mocks.restoreTable,
}));

vi.mock('../../services/PersistentWebSocket', () => ({
  persistentWebSocket: {
    onGlobalEvent: (cb: GlobalListener) => {
      mocks.global.add(cb);
      return () => mocks.global.delete(cb);
    },
  },
}));

function emitTableChanged(tableId = 't1') {
  for (const cb of mocks.global) cb({ type: 'table_changed', table_id: tableId, updated_at: 'x', schema_version: 2 });
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

// The change log's timestamp format (zoned UTC, microseconds).
const CREATED_AT = '2026-10-01T12:00:00.000000+00:00';

vi.mock('./TableRowsDiffPreview', () => ({
  TableRowsDiffPreview: ({ diff }: { diff: TableRowsDiff }) => (
    <pre data-testid="rows-diff">{JSON.stringify(diff)}</pre>
  ),
}));

vi.mock('./TableSchemaDiffPreview', () => ({
  TableSchemaDiffPreview: ({ diff }: { diff: TableSchemaDiff }) => (
    <pre data-testid="schema-diff">{JSON.stringify(diff)}</pre>
  ),
}));

const ACCESS: TableDetail['access'] = {
  can_read: true, rows: 'free', schema: 'approval', can_edit: true, can_restore: true,
  can_rename: true, can_delete: true, can_share: true, can_edit_schema: true,
  can_require_approval: true, can_download_history: true, deny_reason: null,
  schema_deny_reason: null,
};

function table(overrides: Partial<TableDetail> = {}): TableDetail {
  return {
    id: 't1', owner_id: 1, project_id: null, title: 'Leads', description: '', mode: 'private',
    schema_version: 2, row_count: 3, last_write_source: 'ui:1', require_approval: false,
    created_at: '2026-10-01T00:00:00', updated_at: '2026-10-02T00:00:00', scope: 'user',
    column_count: 2, shared: false, shared_with_me: false, permission: null, owner: null,
    shares: [], last_write_user: null, access: ACCESS, schema: [], last_write_conversation: null,
    ...overrides,
  };
}

function change(id: number, overrides: Partial<TableChange> = {}): TableChange {
  return {
    id, created_at: CREATED_AT, source: 'ui:1', source_label: 'you', conversation: null,
    actor: { user_id: 1, name: 'Ada' }, kind: 'rows', op: 'insert', summary: `Inserted ${id} rows`,
    row_count: id, in_effect: true, restore_to: null, schema_before: 1, schema_after: 1,
    size_bytes: 10,
    ...overrides,
  };
}

function col(key: string, name: string, overrides: Partial<TableChangeColumn> = {}): TableChangeColumn {
  return { key, name, type: 'text', multiple: false, options: null, precision: null, current: true, ...overrides };
}

function detailOf(entry: TableChange, overrides: Partial<TableChangeDetail> = {}): TableChangeDetail {
  return {
    ...entry,
    columns: [col('c_a', 'Name'), col('c_b', 'Tags', { type: 'enum', multiple: true })],
    before: [], after: [], truncated: { before: false, after: false },
    before_total: 0, after_total: 0, schema_diff: null, operations: null,
    row_ids: [], row_ids_total: 0,
    ...overrides,
  };
}

function listPage(changes: TableChange[], hasMore = false): ListTableChangesResponse {
  return { changes, has_more: hasMore, next_before_id: hasMore ? changes[changes.length - 1].id : null };
}

// #5 restore to #2 (in effect), #4 and #3 rewound, #2 / #1 in effect.
const LOG: TableChange[] = [
  change(5, { kind: 'restore', op: 'restore', summary: 'Restored to #2', source: 'revert:2', source_label: 'Restored to #2', restore_to: 2, row_count: null }),
  change(4, { in_effect: false, kind: 'schema', op: 'schema', summary: 'Added column Status', row_count: null }),
  change(3, {
    in_effect: false, source: 'conversation:c9', source_label: 'Lead import',
    conversation: { id: 'c9', title: 'Lead import', project_id: null },
  }),
  change(2, { op: 'update', summary: 'Updated 2 rows', row_count: 2 }),
  change(1, { kind: 'schema', op: 'schema', summary: 'Created table', row_count: null }),
];

function renderHistory(props: Partial<Parameters<typeof TableHistory>[0]> = {}) {
  const onClose = vi.fn();
  const onRestored = vi.fn();
  const utils = render(
    <MemoryRouter>
      <TableHistory table={table()} onClose={onClose} onRestored={onRestored} {...props} />
    </MemoryRouter>,
  );
  return { ...utils, onClose, onRestored };
}

beforeEach(() => {
  mocks.listTableChanges.mockReset();
  mocks.getTableChange.mockReset();
  mocks.restoreTable.mockReset();
  mocks.global.clear();
});

afterEach(cleanup);

// --- Pure helpers ----------------------------------------------------------

describe('changeDetailToRowsDiff', () => {
  it('maps an insert to added rows, keys to names, timestamps dropped', () => {
    const d = detailOf(change(7), {
      before: [],
      after: [
        { _id: 10, _created_at: 'a', _updated_at: 'b', c_a: 'Ada', c_b: ['x'] },
        { _id: 11, _created_at: 'a', _updated_at: 'b', c_a: 'Bob', c_b: [] },
      ],
      after_total: 2, row_ids_total: 2,
    });
    expect(changeDetailToRowsDiff(d)).toEqual({
      columns: ['Name', 'Tags'],
      rows: [
        { _id: 10, status: 'added', before: null, after: { Name: 'Ada', Tags: ['x'] }, changed_columns: [] },
        { _id: 11, status: 'added', before: null, after: { Name: 'Bob', Tags: [] }, changed_columns: [] },
      ],
      total: 2,
      truncated: false,
    });
  });

  it('maps an update to changed rows with deep-compared changed_columns', () => {
    const d = detailOf(change(7, { op: 'update' }), {
      before: [{ _id: 3, c_a: 'Ada', c_b: ['x', 'y'] }, { _id: 4, c_a: 'Bob', c_b: ['x'] }],
      after: [{ _id: 3, c_a: 'Ada', c_b: ['x', 'y'] }, { _id: 4, c_a: 'Bobby', c_b: ['y'] }],
      before_total: 2, after_total: 2, row_ids_total: 2,
    });
    const diff = changeDetailToRowsDiff(d);
    expect(diff.rows.map((r) => [r._id, r.status, r.changed_columns])).toEqual([
      [3, 'changed', []],
      [4, 'changed', ['Name', 'Tags']],
    ]);
    expect(diff.rows[1].before).toEqual({ Name: 'Bob', Tags: ['x'] });
  });

  it('maps a delete to deleted rows', () => {
    const d = detailOf(change(7, { op: 'delete' }), {
      before: [{ _id: 8, c_a: 'Gone' }],
      after: [],
      before_total: 1, row_ids_total: 1,
    });
    const diff = changeDetailToRowsDiff(d);
    expect(diff.columns).toEqual(['Name']);
    expect(diff.rows).toEqual([
      { _id: 8, status: 'deleted', before: { Name: 'Gone' }, after: null, changed_columns: [] },
    ]);
    expect(diff.total).toBe(1);
  });

  it('maps an upsert: existing rows changed, new rows added', () => {
    const d = detailOf(change(7, { op: 'upsert' }), {
      before: [{ _id: 2, c_a: 'Old' }],
      after: [{ _id: 2, c_a: 'New' }, { _id: 9, c_a: 'Fresh' }],
      before_total: 1, after_total: 2, row_ids_total: 2,
    });
    const diff = changeDetailToRowsDiff(d);
    expect(diff.rows.map((r) => [r._id, r.status, r.changed_columns])).toEqual([
      [2, 'changed', ['Name']],
      [9, 'added', []],
    ]);
    expect(diff.total).toBe(2);
    expect(diff.truncated).toBe(false);
  });

  it('labels a key no longer in the schema instead of showing it', () => {
    const d = detailOf(change(7), {
      columns: [col('c_a', 'Name'), col('c_dead0001', 'c_dead0001', { type: null, current: false })],
      after: [{ _id: 1, c_a: 'Ada', c_dead0001: 'old' }],
      after_total: 1, row_ids_total: 1,
    });
    const diff = changeDetailToRowsDiff(d);
    expect(diff.columns).toEqual(['Name', REMOVED_COLUMN_LABEL]);
    expect(JSON.stringify(diff)).not.toContain('c_dead0001');
    expect(diff.rows[0].after).toEqual({ Name: 'Ada', [REMOVED_COLUMN_LABEL]: 'old' });
  });

  it('cuts unpairable rows past a truncated image and reports the full total', () => {
    const d = detailOf(change(7, { op: 'update' }), {
      before: [{ _id: 1, c_a: 'a' }],
      after: [{ _id: 1, c_a: 'b' }, { _id: 2, c_a: 'c' }],
      truncated: { before: true, after: false },
      before_total: 500, after_total: 500, row_ids_total: 500,
    });
    const diff = changeDetailToRowsDiff(d);
    // #2 is past the before cut: it is not reported as added.
    expect(diff.rows.map((r) => [r._id, r.status])).toEqual([[1, 'changed']]);
    expect(diff.total).toBe(500);
    expect(diff.truncated).toBe(true);
  });
});

describe('helpers', () => {
  it('columnLabels keeps labels distinct', () => {
    const labels = columnLabels([
      col('c_1', 'c_1', { type: null }),
      col('c_2', 'c_2', { type: null }),
      col('c_3', 'Name'),
    ]);
    expect([...labels.values()]).toEqual([REMOVED_COLUMN_LABEL, `${REMOVED_COLUMN_LABEL} 2`, 'Name']);
  });

  it('droppedValueCounts names columns via detail.columns', () => {
    const d = detailOf(change(4, { kind: 'schema' }), {
      columns: [col('c_s', 'Status')],
      before: { columns: [], title: 'Leads', dropped_values: { c_s: { '1': 'a', '2': 'b' } } },
      after: { columns: [], title: 'Leads' },
    });
    expect(droppedValueCounts(d)).toEqual([{ name: 'Status', count: 2 }]);
  });

  it('restoreImpact counts the in-effect data changes the target lacks', () => {
    // Target #1: #2 counts; #3 / #4 are rewound already; #5 is a restore.
    expect(restoreImpact(LOG, 1)).toEqual({ count: 1, exact: true, newestId: 5 });
    // An older branch brought back by a restore: going to #4 rewinds #2.
    const branch = [
      change(5, { kind: 'restore', restore_to: 2 }),
      change(4),
      change(3, { kind: 'restore', restore_to: 1 }),
      change(2),
      change(1),
    ];
    expect(restoreImpact(branch, 4).count).toBe(1);
    // Partial pages: exact while both paths leave them at the same id.
    expect(restoreImpact([change(10), change(9)], 9)).toEqual({ count: 1, exact: true, newestId: 10 });
    expect(restoreImpact([change(10, { kind: 'restore', restore_to: 3 }), change(9)], 9).exact).toBe(false);
  });

  it('isCurrentState follows restore targets', () => {
    expect(isCurrentState(LOG, 5)).toBe(true);
    expect(isCurrentState(LOG, 2)).toBe(true);
    expect(isCurrentState(LOG, 3)).toBe(false);
    expect(isCurrentState(LOG, 1)).toBe(false);
  });

  it('restoreConfirmText names the count, the target and the undo', () => {
    const plain = [change(13), change(12), change(11), change(10)];
    expect(restoreConfirmText(plain, plain[3])).toEqual([
      'This rewinds 3 changes.',
      'The table goes back to how it was right after #10.',
      'You can undo this by restoring to #13 afterwards.',
    ]);
    expect(restoreConfirmText(LOG, LOG[1])).toEqual([
      '#4 was rewound by a later restore; this brings it back.',
      'The table goes back to how it was right after #4.',
      'You can undo this by restoring to #5 afterwards.',
    ]);
  });

  it('restoreConfirmText says "at least" for a count past the loaded pages, noun by count', () => {
    // #10 restored to #3 (not loaded): the paths leave the pages at #3 and #8.
    const partial = [change(11), change(10, { kind: 'restore', restore_to: 3 }), change(9)];
    expect(restoreImpact(partial, 9)).toEqual({ count: 1, exact: false, newestId: 11 });
    expect(restoreConfirmText(partial, partial[2])[0]).toBe('This rewinds at least 1 change.');
    const more = [change(12), ...partial];
    expect(restoreConfirmText(more, more[3])[0]).toBe('This rewinds at least 2 changes.');
  });

  it('createdColumns keeps named, typed column dicts', () => {
    const d = detailOf(change(1, { kind: 'schema' }), {
      after: { op: 'create_table', columns: [{ key: 'c_a', name: 'Name', type: 'text' }, null, { name: 'x' }] },
    });
    expect(createdColumns(d)).toEqual([{ key: 'c_a', name: 'Name', type: 'text' }]);
    expect(createdColumns(detailOf(change(1)))).toEqual([]);
  });

  it('actorLabel names the actor only when the label does not', () => {
    const t = table();
    expect(actorLabel(change(1), t)).toBeNull();
    expect(actorLabel(change(1, { source: 'action_request:4', source_label: 'Request #4', actor: { user_id: 2, name: 'Bea' } }), t)).toBe('Bea');
    expect(actorLabel(change(1, { source: 'revert:1', source_label: 'Restored to #1' }), t)).toBe('you');
  });
});

// --- Component -------------------------------------------------------------

describe('TableHistory list', () => {
  it('renders in-effect, rewound and restore entries with badges and links', async () => {
    mocks.listTableChanges.mockResolvedValue(listPage(LOG));
    renderHistory();
    const list = await screen.findByRole('list');
    const items = within(list).getAllByRole('listitem');
    expect(items).toHaveLength(5);

    // Restore entry.
    expect(items[0].textContent).toContain('#5');
    expect(items[0].textContent).toContain('Restored to #2');
    expect(within(items[0]).getByText('Restore').classList.contains('table-history-badge--restore')).toBe(true);
    expect(within(items[0]).getByRole('button', { name: 'Show #2' })).toBeTruthy();

    // Rewound entries: struck through with an accessible hint.
    expect(items[1].classList.contains('is-rewound')).toBe(true);
    expect(items[1].getAttribute('title')).toBe('Rewound by a later restore');
    expect(within(items[1]).getByText(/Rewound by a later restore/).classList.contains('table-history-sr-only')).toBe(true);
    expect(within(items[1]).getByText('Schema')).toBeTruthy();
    expect(items[2].classList.contains('is-rewound')).toBe(true);

    // Conversation link.
    const link = within(items[2]).getByRole('link', { name: 'Lead import' });
    expect(link.getAttribute('href')).toBe('/chats/c9');

    // In effect: no strike-through; rows count shown.
    expect(items[3].classList.contains('is-rewound')).toBe(false);
    expect(items[3].textContent).toContain('Updated 2 rows');
    expect(items[3].textContent).toContain('2 rows');
    expect(within(items[3]).getByText('Rows')).toBeTruthy();
  });

  it('pages with Load more using before_id', async () => {
    mocks.listTableChanges.mockResolvedValueOnce(listPage([change(3), change(2)], true));
    renderHistory();
    await screen.findByText('Inserted 3 rows');
    mocks.listTableChanges.mockResolvedValueOnce(listPage([change(1)], false));
    fireEvent.click(screen.getByRole('button', { name: 'Load more' }));
    await screen.findByText('Inserted 1 rows');
    expect(mocks.listTableChanges).toHaveBeenLastCalledWith('t1', { beforeId: 2, limit: 50 });
    expect(screen.queryByRole('button', { name: 'Load more' })).toBeNull();
  });

  it('closes on Escape and from the close button', async () => {
    mocks.listTableChanges.mockResolvedValue(listPage(LOG));
    const { onClose } = renderHistory();
    await screen.findByRole('list');
    fireEvent.keyDown(screen.getByRole('heading', { name: 'History' }), { key: 'Escape' });
    expect(onClose).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole('button', { name: 'Close history' }));
    expect(onClose).toHaveBeenCalledTimes(2);
  });

  it('shows the lost-access state on a 403 and when can_edit turns false', async () => {
    mocks.listTableChanges.mockRejectedValue(new ApiClientError('nope', 403, 'forbidden'));
    const { unmount } = renderHistory();
    expect(await screen.findByText('You no longer have edit access to this table.')).toBeTruthy();
    unmount();

    mocks.listTableChanges.mockReset();
    renderHistory({ table: table({ access: { ...ACCESS, can_edit: false, can_restore: false } }) });
    expect(screen.getByText('You no longer have edit access to this table.')).toBeTruthy();
    expect(mocks.listTableChanges).not.toHaveBeenCalled();
  });

  it('shows the lost-access state on a 404 table_not_found (deleted or unshared)', async () => {
    mocks.listTableChanges.mockRejectedValue(new ApiClientError('Table not found.', 404, 'table_not_found'));
    const { unmount } = renderHistory();
    expect(await screen.findByText('You no longer have edit access to this table.')).toBeTruthy();
    expect(screen.queryByText('Table not found.')).toBeNull();
    unmount();

    // From the detail call too.
    mocks.listTableChanges.mockReset();
    mocks.listTableChanges.mockResolvedValue(listPage(LOG));
    mocks.getTableChange.mockRejectedValue(new ApiClientError('Table not found.', 404, 'table_not_found'));
    renderHistory();
    fireEvent.click(await screen.findByRole('button', { name: /Updated 2 rows/ }));
    expect(await screen.findByText('You no longer have edit access to this table.')).toBeTruthy();
    expect(screen.queryByText('Table not found.')).toBeNull();
  });

  it('reads the zoned change-log timestamps', async () => {
    mocks.listTableChanges.mockResolvedValue(listPage(LOG));
    renderHistory();
    const items = within(await screen.findByRole('list')).getAllByRole('listitem');
    const time = items[0].querySelector('time') as HTMLTimeElement;
    expect(time.getAttribute('dateTime')).toBe(CREATED_AT);
    expect(time.textContent).not.toMatch(/Invalid/);
    expect(time.getAttribute('title')).toBe(new Date(Date.UTC(2026, 9, 1, 12)).toLocaleString());
  });

  it('shows the loading state on Retry and the error again on a second failure', async () => {
    mocks.listTableChanges.mockRejectedValueOnce(new ApiClientError('Server down', 500));
    renderHistory();
    expect(await screen.findByText('Server down')).toBeTruthy();

    const retry = deferred<ListTableChangesResponse>();
    mocks.listTableChanges.mockReturnValueOnce(retry.promise);
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    expect(screen.getByText('Loading history...')).toBeTruthy();
    expect(screen.queryByText('Server down')).toBeNull();
    await act(async () => retry.reject(new ApiClientError('Still down', 500)));
    expect(await screen.findByText('Still down')).toBeTruthy();

    mocks.listTableChanges.mockResolvedValueOnce(listPage(LOG));
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    expect(await screen.findByRole('list')).toBeTruthy();
  });
});

describe('TableHistory detail', () => {
  it('loads a rows change and hands the mapped diff to TableRowsDiffPreview', async () => {
    mocks.listTableChanges.mockResolvedValue(listPage(LOG));
    mocks.getTableChange.mockResolvedValue(
      detailOf(LOG[3], {
        before: [{ _id: 1, c_a: 'a' }], after: [{ _id: 1, c_a: 'b' }],
        before_total: 1, after_total: 1, row_ids_total: 1,
      }),
    );
    renderHistory();
    fireEvent.click(await screen.findByRole('button', { name: /Updated 2 rows/ }));
    const pre = await screen.findByTestId('rows-diff');
    expect(mocks.getTableChange).toHaveBeenCalledWith('t1', 2);
    const diff = JSON.parse(pre.textContent ?? '') as TableRowsDiff;
    expect(diff.rows[0]).toMatchObject({ _id: 1, status: 'changed', changed_columns: ['Name'] });
  });

  it('renders a schema change through TableSchemaDiffPreview with the dropped-values note', async () => {
    mocks.listTableChanges.mockResolvedValue(listPage(LOG));
    const schemaDiff: TableSchemaDiff = {
      columns: [], order_changed: false, before_order: [], after_order: [], changed: true,
    };
    mocks.getTableChange.mockResolvedValue(
      detailOf(LOG[1], {
        columns: [col('c_s', 'Status')],
        before: { columns: [], title: 'Leads', dropped_values: { c_s: { '1': 'x' } } },
        after: { columns: [], title: 'Leads', operations: [] },
        schema_diff: schemaDiff,
      }),
    );
    renderHistory();
    fireEvent.click(await screen.findByRole('button', { name: /Added column Status/ }));
    expect(await screen.findByTestId('schema-diff')).toBeTruthy();
    expect(screen.getByText(/Dropped values/).textContent).toContain('Status: 1 value');
  });

  it('lists the created columns for change #1 through the create_table column table', async () => {
    mocks.listTableChanges.mockResolvedValue(listPage(LOG));
    const stored = (key: string, name: string, extra: Record<string, unknown> = {}) => ({
      key, name, type: 'text', required: false, unique: false, default: null, options: null,
      multiple: false, precision: null, description: '', ...extra,
    });
    mocks.getTableChange.mockResolvedValue(
      detailOf(LOG[4], {
        before: {},
        after: {
          op: 'create_table', title: 'Leads',
          columns: [
            stored('c_a', 'Company', { required: true }),
            stored('c_b', 'Stage', { type: 'enum', multiple: true, options: ['New', 'Won'] }),
          ],
        },
        // The server diffs [] against the columns: every column "added".
        schema_diff: { columns: [], order_changed: false, before_order: [], after_order: [], changed: true },
      }),
    );
    renderHistory();
    fireEvent.click(await screen.findByRole('button', { name: /Created table/ }));
    expect(await screen.findByText('Created the table with these columns:')).toBeTruthy();
    const rows = within(screen.getByRole('table')).getAllByRole('row');
    expect(rows).toHaveLength(3);
    expect(rows[1].textContent).toContain('Company');
    expect(rows[1].textContent).toContain('yes');
    expect(rows[2].textContent).toContain('enum[]');
    expect(rows[2].textContent).toContain('Won');
    expect(screen.queryByTestId('schema-diff')).toBeNull();
    expect(document.body.textContent).not.toContain('c_a');
  });

  it('says "at least" once for cut dropped values', async () => {
    mocks.listTableChanges.mockResolvedValue(listPage(LOG));
    mocks.getTableChange.mockResolvedValue(
      detailOf(LOG[1], {
        columns: [col('c_s', 'Status'), col('c_n', 'Notes')],
        before: { columns: [], title: 'Leads', dropped_values: { c_s: { '1': 'x', '2': 'y' }, c_n: { '1': 'z' } } },
        after: { columns: [], title: 'Leads', operations: [] },
        truncated: { before: true, after: false },
        schema_diff: { columns: [], order_changed: false, before_order: [], after_order: [], changed: true },
      }),
    );
    renderHistory();
    fireEvent.click(await screen.findByRole('button', { name: /Added column Status/ }));
    const note = (await screen.findByText(/Dropped values/)).textContent ?? '';
    expect(note).toContain('Dropped values (at least): Status: 2 values, Notes: 1 value.');
    expect(note.match(/at least/g)).toHaveLength(1);
  });

  it('says a rows change whose images were cut to nothing is too large to show', async () => {
    mocks.listTableChanges.mockResolvedValue(listPage(LOG));
    mocks.getTableChange.mockResolvedValue(
      detailOf(LOG[3], {
        before: [], after: [], truncated: { before: true, after: true },
        before_total: 2, after_total: 2, row_ids_total: 2,
      }),
    );
    renderHistory();
    fireEvent.click(await screen.findByRole('button', { name: /Updated 2 rows/ }));
    expect(await screen.findByText('This change is too large to show here (2 rows).')).toBeTruthy();
    expect(screen.queryByText(/No row images/)).toBeNull();
  });

  it('caches details per id and drops them when the schema version changes', async () => {
    mocks.listTableChanges.mockResolvedValue(listPage(LOG));
    mocks.getTableChange.mockImplementation((_id: string, changeId: number) =>
      Promise.resolve(detailOf(LOG.find((c) => c.id === changeId) as TableChange)),
    );
    const { rerender, onClose, onRestored } = renderHistory();
    fireEvent.click(await screen.findByRole('button', { name: /Updated 2 rows/ }));
    await screen.findByRole('article', { name: 'Change #2' });
    await screen.findByText(/No row images/);
    fireEvent.click(screen.getByRole('button', { name: /Inserted 3 rows/ }));
    await screen.findByRole('article', { name: 'Change #3' });
    await screen.findByText(/No row images/);
    fireEvent.click(screen.getByRole('button', { name: /Updated 2 rows/ }));
    expect(screen.getByText(/No row images/)).toBeTruthy();
    expect(mocks.getTableChange).toHaveBeenCalledTimes(2);

    // Row images are decoded with the current schema: a new version re-reads.
    rerender(
      <MemoryRouter>
        <TableHistory table={table({ schema_version: 3 })} onClose={onClose} onRestored={onRestored} />
      </MemoryRouter>,
    );
    await waitFor(() => expect(mocks.getTableChange).toHaveBeenCalledTimes(3));
    expect(mocks.getTableChange).toHaveBeenLastCalledWith('t1', 2);
  });

  it('resets the selection when the table id changes', async () => {
    mocks.listTableChanges.mockResolvedValue(listPage(LOG));
    mocks.getTableChange.mockResolvedValue(detailOf(LOG[3]));
    const { rerender, onClose, onRestored } = renderHistory();
    fireEvent.click(await screen.findByRole('button', { name: /Updated 2 rows/ }));
    await screen.findByRole('article', { name: 'Change #2' });
    rerender(
      <MemoryRouter>
        <TableHistory table={table({ id: 't2' })} onClose={onClose} onRestored={onRestored} />
      </MemoryRouter>,
    );
    expect(await screen.findByText('Select a change to see what it did.')).toBeTruthy();
    expect(screen.queryByRole('article')).toBeNull();
    await waitFor(() => expect(mocks.listTableChanges).toHaveBeenLastCalledWith('t2', { limit: 50 }));
  });

  it('shows a restore entry target and a load error with retry', async () => {
    mocks.listTableChanges.mockResolvedValue(listPage(LOG));
    mocks.getTableChange.mockRejectedValueOnce(new ApiClientError('Server down', 500));
    renderHistory();
    fireEvent.click(await screen.findByRole('button', { name: /^#5/ }));
    expect(await screen.findByText('Server down')).toBeTruthy();
    mocks.getTableChange.mockResolvedValueOnce(detailOf(LOG[0], { before: {}, after: { restore_to: 2 } }));
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    expect(await screen.findByText(/Restored the table to/)).toBeTruthy();
  });
});

describe('TableHistory restore', () => {
  async function openDetail(entryName: RegExp, entry: TableChange, props = {}) {
    mocks.listTableChanges.mockResolvedValue(listPage(LOG));
    mocks.getTableChange.mockResolvedValue(detailOf(entry));
    const utils = renderHistory(props);
    fireEvent.click(await screen.findByRole('button', { name: entryName }));
    await screen.findByRole('article', { name: `Change #${entry.id}` });
    return utils;
  }

  it('hides the button without can_restore', async () => {
    await openDetail(/Inserted 3 rows/, LOG[2], {
      table: table({ access: { ...ACCESS, can_restore: false } }),
    });
    expect(await screen.findByText('No row images were recorded for this change.')).toBeTruthy();
    expect(screen.queryByRole('button', { name: /Restore table to this point/ })).toBeNull();
  });

  it('disables the button when the table is already in that state', async () => {
    await openDetail(/Updated 2 rows/, LOG[3]);
    expect((screen.getByRole('button', { name: /Restore table to this point/ }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByText('The table is already in this state.')).toBeTruthy();
  });

  it('confirms with the rewound count, restores, reports and re-lists', async () => {
    const { onRestored } = await openDetail(/Created table/, LOG[4]);
    fireEvent.click(screen.getByRole('button', { name: /Restore table to this point/ }));
    const dialog = await screen.findByRole('dialog');
    expect(dialog.textContent).toContain('This rewinds 1 change.');
    expect(dialog.textContent).toContain('right after #1');
    expect(dialog.textContent).toContain('restoring to #5 afterwards');

    const res: RestoreTableResponse = {
      change: change(6, { kind: 'restore', op: 'restore', restore_to: 1, summary: 'Restored to #1' }),
      table: table({ updated_at: '2026-10-03T00:00:00' }),
      schema_version: 1, row_count: 0, restored_to: 1,
    };
    mocks.restoreTable.mockResolvedValue(res);
    mocks.listTableChanges.mockResolvedValue(listPage([res.change as TableChange, ...LOG]));
    mocks.getTableChange.mockResolvedValue(detailOf(res.change as TableChange, { before: {}, after: { restore_to: 1 } }));
    fireEvent.click(within(dialog).getByRole('button', { name: 'Restore' }));
    await waitFor(() => expect(onRestored).toHaveBeenCalledWith(res));
    expect(mocks.restoreTable).toHaveBeenCalledWith('t1', 1);
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    // The new restore entry is listed and selected.
    expect(await screen.findByRole('article', { name: 'Change #6' })).toBeTruthy();
  });

  it('puts the new entry on top at once and moves focus to it after a restore', async () => {
    const { onClose } = await openDetail(/Created table/, LOG[4]);
    fireEvent.click(screen.getByRole('button', { name: /Restore table to this point/ }));
    const dialog = await screen.findByRole('dialog');
    const made = change(6, { kind: 'restore', op: 'restore', restore_to: 1, summary: 'Restored to #1', row_count: null });
    mocks.restoreTable.mockResolvedValue({
      change: made, table: table(), schema_version: 1, row_count: 0, restored_to: 1,
    } satisfies RestoreTableResponse);
    // The re-list never lands: what shows comes from the restore response.
    mocks.listTableChanges.mockReturnValue(new Promise(() => {}));
    mocks.getTableChange.mockResolvedValue(detailOf(made, { before: {}, after: { restore_to: 1 } }));
    fireEvent.click(within(dialog).getByRole('button', { name: 'Restore' }));

    await waitFor(() => expect(document.activeElement?.getAttribute('data-change-id')).toBe('6'));
    const items = within(screen.getByRole('list')).getAllByRole('listitem');
    expect(items[0].textContent).toContain('Restored to #1');
    // The newest entry: restoring to it would change nothing.
    await screen.findByText(/Restored the table to/);
    expect((screen.getByRole('button', { name: /Restore table to this point/ }) as HTMLButtonElement).disabled).toBe(true);
    // Focus is in the drawer, so Escape still closes it.
    fireEvent.keyDown(document.activeElement as Element, { key: 'Escape' });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('closes only the confirm on Escape inside it', async () => {
    const { onClose } = await openDetail(/Created table/, LOG[4]);
    fireEvent.click(screen.getByRole('button', { name: /Restore table to this point/ }));
    const dialog = await screen.findByRole('dialog');
    fireEvent.keyDown(within(dialog).getByRole('button', { name: 'Cancel' }), { key: 'Escape' });
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(onClose).not.toHaveBeenCalled();
    expect(screen.getByRole('article', { name: 'Change #1' })).toBeTruthy();
  });

  it('reads in_effect for the confirm from the current list entry', async () => {
    await openDetail(/Inserted 3 rows/, LOG[2]);
    fireEvent.click(screen.getByRole('button', { name: /Restore table to this point/ }));
    const dialog = await screen.findByRole('dialog');
    expect(dialog.textContent).toContain('#3 was rewound by a later restore');

    // A restore to #4 elsewhere brings #3 back while the confirm is open.
    mocks.listTableChanges.mockResolvedValue(
      listPage([
        change(6, { kind: 'restore', op: 'restore', summary: 'Restored to #4', restore_to: 4, row_count: null }),
        { ...LOG[0], in_effect: false },
        { ...LOG[1], in_effect: true },
        { ...LOG[2], in_effect: true },
        LOG[3],
        LOG[4],
      ]),
    );
    act(() => emitTableChanged());
    await waitFor(() => expect(dialog.textContent).not.toContain('was rewound'));
    expect(dialog.textContent).toContain('restoring to #6 afterwards');
  });

  it('shows a restore error in the dialog', async () => {
    await openDetail(/Created table/, LOG[4]);
    fireEvent.click(screen.getByRole('button', { name: /Restore table to this point/ }));
    const dialog = await screen.findByRole('dialog');
    mocks.restoreTable.mockRejectedValue(new ApiClientError('There is no change #1.', 404, 'unknown_change'));
    fireEvent.click(within(dialog).getByRole('button', { name: 'Restore' }));
    expect(await within(dialog).findByText('There is no change #1.')).toBeTruthy();
  });

  it('switches to the lost-access state on a 403 restore', async () => {
    await openDetail(/Created table/, LOG[4]);
    fireEvent.click(screen.getByRole('button', { name: /Restore table to this point/ }));
    const dialog = await screen.findByRole('dialog');
    mocks.restoreTable.mockRejectedValue(new ApiClientError('nope', 403, 'forbidden'));
    fireEvent.click(within(dialog).getByRole('button', { name: 'Restore' }));
    expect(await screen.findByText('You no longer have edit access to this table.')).toBeTruthy();
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
  });
});
