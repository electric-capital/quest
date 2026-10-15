// TableViewer: route-param parsing, the loading / gone / error states, the
// URL as the source of truth for filter and sort (the bar and the headers
// navigate, the viewer reads the route), paging (offset reset on change,
// clamp on a shrinking total), stale columns, and when rows re-query on
// table_changed.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, useLocation } from 'react-router-dom';
import { ApiClientError } from '../../api/request';
import type { TableColumn, TableDetail, TableRowsPage } from '../../api/types';
import type { TableView } from '../../hooks/useTable';
import { parseTablesRoute, tableViewerPath } from '../../utils/tablesRoute';
import { TableViewer, filterColumnNames, parseFilterParam, parseSortParam, resolveSort } from './TableViewer';

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

/** The app's wiring: the viewer gets its props from the current location. */
function RouteHost() {
  const location = useLocation();
  const route = parseTablesRoute(location.pathname, location.search);
  return (
    <>
      <output data-testid="location">{location.pathname + location.search}</output>
      {route?.kind === 'viewer' && (
        <TableViewer tableId={route.tableId} filter={route.filter} sort={route.sort} />
      )}
    </>
  );
}

function ui(props: { filter?: string | null; sort?: string | null; extra?: string } = {}) {
  let path = tableViewerPath('t1', { filter: props.filter, sort: props.sort });
  if (props.extra) path += (path.includes('?') ? '&' : '?') + props.extra;
  return (
    <MemoryRouter initialEntries={[path]}>
      <RouteHost />
    </MemoryRouter>
  );
}

/** The current location's search params. */
function currentParams() {
  const text = screen.getByTestId('location').textContent ?? '';
  return new URLSearchParams(text.slice(text.indexOf('?') + 1 || text.length));
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

  it('resolveSort keeps every existing key, in order, schema spelling', () => {
    expect(
      resolveSort('[{"column":"status","direction":"desc"},{"column":"Name"},{"column":"_ID"}]', COLUMNS),
    ).toEqual({
      keys: [
        { column: 'Status', direction: 'desc' },
        { column: 'Name', direction: 'asc' },
        { column: '_id', direction: 'asc' },
      ],
      missing: [],
      unreadable: false,
    });
    expect(resolveSort('[{"column":"Gone"},{"column":"Name","direction":"desc"}]', COLUMNS)).toEqual({
      keys: [{ column: 'Name', direction: 'desc' }],
      missing: ['Gone'],
      unreadable: false,
    });
    expect(resolveSort('[{"column":"Gone"}]', COLUMNS)).toEqual({ keys: null, missing: ['Gone'], unreadable: false });
    // Names differing only in case are one column: the server refuses it.
    expect(resolveSort('[{"column":"Name"},{"column":"NAME"}]', COLUMNS)).toEqual({
      keys: null, missing: [], unreadable: true,
    });
    expect(resolveSort('[{"column":"Name","direction":"up"}]', COLUMNS).unreadable).toBe(true);
    expect(resolveSort(null, COLUMNS)).toEqual({ keys: null, missing: [], unreadable: false });
    expect(resolveSort('[{"column":"Gone"}]', null)).toEqual({
      keys: [{ column: 'Gone', direction: 'asc' }], missing: [], unreadable: false,
    });
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
    expect(screen.getByText('This sort references a column that no longer exists (Gone).')).toBeTruthy();
    cleanup();
    render(ui({ filter: '{"column":"Status","op":"eq","value":"open"}' }));
    await screen.findByText('row at 0');
    expect(lastQuery()).toMatchObject({ filter: { column: 'Status', op: 'eq', value: 'open' } });
    // Footer counts the matches under a filter.
    expect(document.querySelector('footer')?.textContent).toContain('350 rows (350 matching)');
  });

  it('sends every key of a multi-key sort and shows the primary arrow', async () => {
    render(ui({ sort: '[{"column":"Status","direction":"desc"},{"column":"Name"}]' }));
    await screen.findByText('row at 0');
    expect(lastQuery()).toMatchObject({
      sort: [
        { column: 'Status', direction: 'desc' },
        { column: 'Name', direction: 'asc' },
      ],
    });
    expect(screen.getByRole('button', { name: 'Status' }).closest('th')?.getAttribute('aria-sort')).toBe('descending');
    expect(screen.getByRole('button', { name: 'Name' }).closest('th')?.getAttribute('aria-sort')).toBeNull();
    expect(screen.queryByText(/sort/i, { selector: '.table-filter-note' })).toBeNull();
    // A header click replaces the sort with that one key.
    fireEvent.click(screen.getByRole('button', { name: 'Name' }));
    await waitFor(() => expect(currentParams().get('sort')).toBe('[{"column":"Name","direction":"asc"}]'));
    await waitFor(() => expect(lastQuery()).toMatchObject({ sort: [{ column: 'Name', direction: 'asc' }] }));
  });

  it('leaves out a sort key on a dropped column, names it, and clears', async () => {
    render(ui({ sort: '[{"column":"Gone","direction":"desc"},{"column":"Name","direction":"desc"}]' }));
    await screen.findByText('row at 0');
    expect(lastQuery()).toMatchObject({ sort: [{ column: 'Name', direction: 'desc' }] });
    expect(screen.getByText('This sort references a column that no longer exists (Gone).')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Clear sort' }));
    await waitFor(() => expect(currentParams().has('sort')).toBe(false));
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Clear sort' })).toBeNull());
  });

  it('an unreadable sort (a column twice) is not sent and says so', async () => {
    render(ui({ sort: '[{"column":"Name"},{"column":"name","direction":"desc"}]' }));
    await screen.findByText('row at 0');
    expect(lastQuery()).toMatchObject({ sort: null });
    expect(screen.getByText("This link's sort could not be read.")).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Clear sort' }));
    await waitFor(() => expect(currentParams().has('sort')).toBe(false));
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
  it('a sort header click navigates to ?sort= (replace, other params kept)', async () => {
    render(ui({ filter: '{"column":"Status","op":"eq","value":"open"}', extra: 'keep=1' }));
    await screen.findByText('row at 0');
    fireEvent.click(screen.getByRole('button', { name: 'Name' }));
    await waitFor(() => expect(currentParams().get('sort')).toBe('[{"column":"Name","direction":"asc"}]'));
    expect(currentParams().get('keep')).toBe('1');
    expect(currentParams().get('filter')).toBe('{"column":"Status","op":"eq","value":"open"}');
    await waitFor(() =>
      expect(lastQuery()).toMatchObject({ sort: [{ column: 'Name', direction: 'asc' }], offset: 0 }),
    );
    // asc -> desc -> none: the param goes away.
    fireEvent.click(screen.getByRole('button', { name: 'Name' }));
    await waitFor(() => expect(currentParams().get('sort')).toBe('[{"column":"Name","direction":"desc"}]'));
    fireEvent.click(screen.getByRole('button', { name: 'Name' }));
    await waitFor(() => expect(currentParams().has('sort')).toBe(false));
    await waitFor(() => expect(lastQuery()).toMatchObject({ sort: null }));
  });

  it('adding a filter on the bar navigates to ?filter= and re-queries from the first page', async () => {
    render(ui({ sort: '[{"column":"Name","direction":"desc"}]' }));
    await screen.findByText('row at 0');
    fireEvent.click(screen.getByRole('button', { name: 'Next' }));
    await screen.findByText('row at 200');

    fireEvent.click(screen.getByRole('button', { name: 'Add filter' }));
    fireEvent.change(screen.getByLabelText('Column'), { target: { value: 'Status' } });
    fireEvent.change(screen.getByLabelText('Value'), { target: { value: 'won' } });
    fireEvent.click(screen.getByRole('button', { name: 'Apply' }));

    await waitFor(() =>
      expect(currentParams().get('filter')).toBe('{"column":"Status","op":"eq","value":"won"}'),
    );
    expect(currentParams().get('sort')).toBe('[{"column":"Name","direction":"desc"}]');
    await waitFor(() =>
      expect(lastQuery()).toMatchObject({
        filter: { column: 'Status', op: 'eq', value: 'won' },
        sort: [{ column: 'Name', direction: 'desc' }],
        offset: 0,
      }),
    );
    expect(screen.getByRole('button', { name: 'Edit filter: Status is won' })).toBeTruthy();

    // Removing the chip clears the param and the filter.
    fireEvent.click(screen.getByRole('button', { name: 'Remove filter: Status is won' }));
    await waitFor(() => expect(currentParams().has('filter')).toBe(false));
    await waitFor(() => expect(lastQuery()).toMatchObject({ filter: null }));
  });

  it('sends a custom filter as-is and offers Clear', async () => {
    const custom = { or: [{ column: 'Status', op: 'eq', value: 'won' }, { column: 'Name', op: 'is_null' }] };
    render(ui({ filter: JSON.stringify(custom) }));
    await screen.findByText('row at 0');
    expect(lastQuery()).toMatchObject({ filter: custom });
    expect(screen.getByText('Custom filter')).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Add filter' })).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Clear' }));
    await waitFor(() => expect(currentParams().has('filter')).toBe(false));
  });

  it('names a filter column the schema no longer has instead of dropping it silently', async () => {
    render(ui({ filter: '{"column":"Gone","op":"is_null"}' }));
    await screen.findByText('row at 0');
    expect(lastQuery()).toMatchObject({ filter: null });
    expect(screen.getByText(/references a column that no longer exists \(Gone\)/)).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Clear' }));
    await waitFor(() => expect(currentParams().has('filter')).toBe(false));
  });

  it('a filter param that is not JSON is not sent and can be cleared', async () => {
    render(ui({ filter: '{oops' }));
    await screen.findByText('row at 0');
    expect(lastQuery()).toMatchObject({ filter: null });
    expect(screen.getByText(/could not be read/)).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Clear' }));
    await waitFor(() => expect(currentParams().has('filter')).toBe(false));
  });

  it('marks the chip a server filter error points at', async () => {
    mocks.queryTableRows.mockRejectedValue(
      new ApiClientError('Unknown option.', 400, 'invalid_value', undefined, {
        detail: { error: 'invalid_value', message: 'Unknown option.', path: 'and[1].value' },
      }),
    );
    const filter = { and: [{ column: 'Name', op: 'contains', value: 'a' }, { column: 'Status', op: 'eq', value: 'won' }] };
    render(ui({ filter: JSON.stringify(filter) }));
    const alert = await screen.findByRole('alert');
    expect(alert.textContent).toBe('This filter cannot be applied: Unknown option.');
    expect(screen.getByRole('button', { name: 'Edit filter: Status is won (invalid)' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Edit filter: Name contains a' })).toBeTruthy();
    // Shown once, on the bar.
    expect(screen.getAllByRole('alert')).toHaveLength(1);
  });

  it('a sort error is a rows error, not a filter error', async () => {
    mocks.queryTableRows.mockRejectedValue(
      new ApiClientError('Bad sort.', 400, 'invalid_sort', undefined, {
        detail: { error: 'invalid_sort', message: 'Bad sort.', path: 'sort[0].direction' },
      }),
    );
    render(ui());
    const alert = await screen.findByRole('alert');
    expect(alert.textContent).toContain('Failed to load rows: Bad sort.');
  });

  it('does not re-query rows on a delete / roster event (updated_at null)', async () => {
    const { rerender } = render(ui());
    await screen.findByText('row at 0');
    expect(mocks.queryTableRows).toHaveBeenCalledTimes(1);
    mocks.view = view({ changeSeq: 1, lastChange: { table_id: 't1', updated_at: null, schema_version: 1 } });
    rerender(ui());
    await act(async () => {});
    expect(mocks.queryTableRows).toHaveBeenCalledTimes(1);
  });

  it('does not re-fetch the detail for a rows page on an older schema version', async () => {
    mocks.queryTableRows.mockImplementation(async () => page(350, 0, 1));
    const refresh = vi.fn(async () => {});
    mocks.view = view({ table: detail({ schema_version: 3 }), refresh });
    render(ui());
    await screen.findByText('row at 0');
    await act(async () => {});
    expect(refresh).not.toHaveBeenCalled();
  });
});
