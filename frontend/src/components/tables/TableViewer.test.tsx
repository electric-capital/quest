// TableViewer: route-param parsing, the loading / gone / error states, sort
// and paging state (offset reset on sort change, clamp on a shrinking
// total), stale columns dropped, and when rows re-query on table_changed.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { ApiClientError } from '../../api/request';
import type { TableColumn, TableDetail, TableRowsPage } from '../../api/types';
import type { TableView } from '../../hooks/useTable';
import { TableViewer, filterColumnNames, parseFilterParam, parseSortParam } from './TableViewer';

const mocks = vi.hoisted(() => ({
  queryTableRows: vi.fn(),
  view: null as unknown as TableView,
}));

vi.mock('../../api/tablesApi', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../api/tablesApi')>()),
  queryTableRows: mocks.queryTableRows,
}));

vi.mock('../../hooks/useTable', () => ({
  useTable: () => mocks.view,
}));

vi.mock('../../contexts/ProjectsContext', () => ({
  useProjects: () => ({ projects: [] }),
}));

const COLUMNS: TableColumn[] = [
  { key: 'c_a', name: 'Name', type: 'text', required: false, unique: false, default: null, options: null, multiple: false, precision: null, description: '' },
  { key: 'c_b', name: 'Status', type: 'enum', required: false, unique: false, default: null, options: ['open', 'won'], multiple: false, precision: null, description: '' },
];

function detail(overrides: Partial<TableDetail> = {}): TableDetail {
  return {
    id: 't1', owner_id: 1, project_id: null, title: 'Leads', description: '', mode: 'private',
    schema_version: 1, row_count: 350, last_write_source: 'ui:1', require_approval: false,
    created_at: '2026-10-01T00:00:00', updated_at: '2026-10-02T00:00:00', scope: 'user',
    column_count: 2, shared: false, shared_with_me: false, permission: null, owner: null,
    shares: [], last_write_user: null,
    access: {
      can_read: true, rows: 'free', schema: 'approval', can_edit: true, can_restore: true,
      can_rename: true, can_delete: true, can_share: true, can_edit_schema: true,
      can_require_approval: true, can_download_history: true, deny_reason: null,
      schema_deny_reason: null,
    },
    schema: COLUMNS, last_write_conversation: null,
    ...overrides,
  };
}

function page(total: number, offset = 0, schemaVersion = 1): TableRowsPage {
  return {
    rows: [{ _id: offset + 1, _updated_at: '2026-10-01T00:00:00', c_a: `row at ${offset}`, c_b: 'open' }],
    total, limit: 200, offset, truncated: offset + 1 < total, schema_version: schemaVersion,
  };
}

function view(overrides: Partial<TableView> = {}): TableView {
  return {
    table: detail(), loading: false, error: null, notFound: false, refresh: vi.fn(async () => {}),
    changeSeq: 0, lastChange: null, applyDetail: vi.fn(), applyRow: vi.fn(),
    ...overrides,
  };
}

function ui(props: { filter?: string | null; sort?: string | null } = {}) {
  return (
    <MemoryRouter>
      <TableViewer tableId="t1" filter={props.filter ?? null} sort={props.sort ?? null} />
    </MemoryRouter>
  );
}

function lastQuery() {
  const calls = mocks.queryTableRows.mock.calls;
  return calls[calls.length - 1][1];
}

describe('route param parsing', () => {
  it('parseSortParam reads the first {column, direction} key', () => {
    expect(parseSortParam('[{"column":"Name","direction":"desc"}]')).toEqual({ column: 'Name', direction: 'desc' });
    expect(parseSortParam('{"column":"Name","direction":"asc"}')).toEqual({ column: 'Name', direction: 'asc' });
    expect(parseSortParam('[{"column":"Name","direction":"up"}]')).toBeNull();
    expect(parseSortParam('not json')).toBeNull();
    expect(parseSortParam(null)).toBeNull();
  });

  it('parseFilterParam accepts a JSON object only', () => {
    expect(parseFilterParam('{"column":"Status","op":"eq","value":"open"}')).toEqual({
      column: 'Status', op: 'eq', value: 'open',
    });
    expect(parseFilterParam('[1]')).toBeNull();
    expect(parseFilterParam('{oops')).toBeNull();
  });

  it('filterColumnNames walks and / or / not', () => {
    expect(
      filterColumnNames({
        and: [{ column: 'A', op: 'is_null' }, { or: [{ not: { column: 'B', op: 'is_null' } }, { column: 'C', op: 'is_null' }] }],
      }),
    ).toEqual(['A', 'B', 'C']);
  });
});

describe('TableViewer', () => {
  beforeEach(() => {
    mocks.queryTableRows.mockReset();
    mocks.queryTableRows.mockImplementation(async (_id: string, opts: { offset?: number }) =>
      page(350, opts.offset ?? 0),
    );
    mocks.view = view();
  });
  afterEach(() => cleanup());

  it('shows loading, gone and error states', async () => {
    mocks.view = view({ table: null, loading: true });
    render(ui());
    expect(screen.getByText('Loading...')).toBeTruthy();
    cleanup();
    mocks.view = view({ table: null, notFound: true });
    render(ui());
    expect(screen.getByText(/no longer exists/)).toBeTruthy();
    expect(screen.getByRole('link', { name: 'All tables' }).getAttribute('href')).toBe('/tables');
    cleanup();
    const refresh = vi.fn(async () => {});
    mocks.view = view({ table: null, error: new ApiClientError('Server down', 500), refresh });
    render(ui());
    expect(screen.getByText('Server down')).toBeTruthy();
    await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Retry' })));
    expect(refresh).toHaveBeenCalled();
    expect(mocks.queryTableRows).not.toHaveBeenCalled();
  });

  it('renders header, grid and footer, sorting from the route param', async () => {
    render(ui({ sort: '[{"column":"Name","direction":"desc"}]' }));
    await screen.findByText('row at 0');
    expect(lastQuery()).toMatchObject({ sort: [{ column: 'Name', direction: 'desc' }], offset: 0, limit: 200 });
    expect(screen.getByRole('button', { name: 'Leads' })).toBeTruthy();
    expect(screen.getByText('Rows 1-200 of 350')).toBeTruthy();
    expect(document.querySelector('footer')?.textContent).toContain('Schema v1');
  });

  it('pages, and a sort change goes back to the first page', async () => {
    render(ui());
    await screen.findByText('row at 0');
    fireEvent.click(screen.getByRole('button', { name: 'Next' }));
    await screen.findByText('row at 200');
    expect(lastQuery()).toMatchObject({ offset: 200, sort: null });
    fireEvent.click(screen.getByRole('button', { name: 'Name' }));
    await waitFor(() => expect(lastQuery()).toMatchObject({ offset: 0, sort: [{ column: 'Name', direction: 'asc' }] }));
  });

  it('steps back to the last page when the total shrinks below the offset', async () => {
    const { rerender } = render(ui());
    await screen.findByText('row at 0');
    fireEvent.click(screen.getByRole('button', { name: 'Next' }));
    await screen.findByText('row at 200');
    // Rows were deleted: a re-query at offset 200 finds only 150 rows.
    mocks.queryTableRows.mockImplementation(async (_id: string, opts: { offset?: number }) =>
      opts.offset === 200 ? { ...page(150, 200), rows: [] } : page(150, opts.offset ?? 0),
    );
    mocks.view = view({ changeSeq: 1, lastChange: { table_id: 't1', updated_at: 'x', schema_version: 1 } });
    rerender(ui());
    await waitFor(() => expect(lastQuery()).toMatchObject({ offset: 0 }));
    await screen.findByText('Rows 1-150 of 150');
  });

  it('drops a sort or filter naming a column the schema lacks', async () => {
    render(ui({ sort: '[{"column":"Gone","direction":"asc"}]', filter: '{"column":"Gone","op":"is_null"}' }));
    await screen.findByText('row at 0');
    expect(lastQuery()).toMatchObject({ sort: null, filter: null });
    cleanup();
    render(ui({ filter: '{"column":"Status","op":"eq","value":"open"}' }));
    await screen.findByText('row at 0');
    expect(lastQuery()).toMatchObject({ filter: { column: 'Status', op: 'eq', value: 'open' } });
    // Footer counts the matches under a filter.
    expect(document.querySelector('footer')?.textContent).toContain('350 rows (350 matching)');
  });

  it('re-queries rows on table_changed only when the schema version matches', async () => {
    const { rerender } = render(ui());
    await screen.findByText('row at 0');
    expect(mocks.queryTableRows).toHaveBeenCalledTimes(1);

    // A row write: same schema version -> re-query.
    mocks.view = view({ changeSeq: 1, lastChange: { table_id: 't1', updated_at: 'a', schema_version: 1 } });
    rerender(ui());
    await waitFor(() => expect(mocks.queryTableRows).toHaveBeenCalledTimes(2));

    // A schema change announced before the detail re-fetch lands: wait.
    mocks.view = view({ changeSeq: 2, lastChange: { table_id: 't1', updated_at: 'b', schema_version: 2 } });
    rerender(ui());
    await act(async () => {});
    expect(mocks.queryTableRows).toHaveBeenCalledTimes(2);

    // The detail with schema v2 arrives: re-query.
    mocks.queryTableRows.mockImplementation(async (_id: string, opts: { offset?: number }) =>
      page(350, opts.offset ?? 0, 2),
    );
    mocks.view = view({
      table: detail({ schema_version: 2 }),
      changeSeq: 2,
      lastChange: { table_id: 't1', updated_at: 'b', schema_version: 2 },
    });
    rerender(ui());
    await waitFor(() => expect(mocks.queryTableRows).toHaveBeenCalledTimes(3));
  });

  it('asks for a detail re-fetch when a rows page is on another schema version', async () => {
    mocks.queryTableRows.mockImplementation(async () => page(350, 0, 5));
    const refresh = vi.fn(async () => {});
    mocks.view = view({ refresh });
    render(ui());
    await screen.findByText('row at 0');
    await waitFor(() => expect(refresh).toHaveBeenCalledTimes(1));
  });

  it('shows a filter error from the rows query', async () => {
    mocks.queryTableRows.mockRejectedValue(
      new ApiClientError('Bad value for Status.', 400, 'invalid_value', undefined, {
        detail: { error: 'invalid_value', message: 'Bad value for Status.', path: 'value' },
      }),
    );
    render(ui({ filter: '{"column":"Status","op":"eq","value":"nope"}' }));
    const alert = await screen.findByRole('alert');
    expect(alert.textContent).toContain('This filter cannot be applied: Bad value for Status.');
  });
});
