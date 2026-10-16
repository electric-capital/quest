import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiClientError } from '../../api/request';
import type { ListTablesOptions } from '../../api/tablesApi';
import type { ListTablesResponse, Project, TableListRow } from '../../api/types';
import {
  EMPTY_HINT,
  PROJECT_EMPTY_HINT,
  PUBLIC_PROJECT_EMPTY_HINT,
  TablesListView,
  emptyHint,
} from './TablesListView';

type GlobalListener = (event: { type: string; [key: string]: unknown }) => void;

const mocks = vi.hoisted(() => ({
  listTables: vi.fn<(opts: ListTablesOptions) => Promise<ListTablesResponse>>(),
  projects: [] as Project[],
  projectsLoaded: true,
  global: new Set<(event: { type: string; [key: string]: unknown }) => void>(),
}));

vi.mock('../../api/tablesApi', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../api/tablesApi')>()),
  listTables: mocks.listTables,
}));

vi.mock('../../services/PersistentWebSocket', () => ({
  persistentWebSocket: {
    onGlobalEvent: (cb: GlobalListener) => {
      mocks.global.add(cb);
      return () => mocks.global.delete(cb);
    },
  },
}));

vi.mock('../../contexts/ProjectsContext', () => ({
  useProjects: () => ({ projects: mocks.projects, projectsLoaded: mocks.projectsLoaded }),
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
    row_count: 12,
    last_write_source: null,
    require_approval: false,
    created_at: '2026-10-01T00:00:00',
    updated_at: '2026-10-01T00:00:00',
    scope: overrides.project_id ? 'project' : 'user',
    column_count: 3,
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

function project(id: string, name: string, overrides: Partial<Project> = {}): Project {
  return {
    id, user_id: 1, name, guide: '', public: false, archived: false,
    created_at: '2026-01-01T00:00:00', updated_at: null, conversation_count: 0, ...overrides,
  };
}

function page(tables: TableListRow[], nextCursor: string | null = null): ListTablesResponse {
  return { tables, has_more: nextCursor !== null, next_cursor: nextCursor };
}

function LocationProbe() {
  const location = useLocation();
  return <div data-testid="location">{location.pathname + location.search}</div>;
}

function renderView(projectId: string | null, onNewTable?: () => void) {
  return render(
    <MemoryRouter initialEntries={['/tables']}>
      <Routes>
        <Route path="*" element={<TablesListView projectId={projectId} onNewTable={onNewTable} />} />
      </Routes>
      <LocationProbe />
    </MemoryRouter>,
  );
}

const groupHeadings = (container: HTMLElement) =>
  [...container.querySelectorAll('.tables-group-heading')].map((h) => h.textContent);

const rowTitles = (root: HTMLElement) =>
  [...root.querySelectorAll('.tables-row-title')].map((el) => el.textContent);

beforeEach(() => {
  mocks.listTables.mockReset();
  mocks.global.clear();
  mocks.projects = [];
  mocks.projectsLoaded = true;
});

afterEach(() => {
  cleanup();
});

describe('TablesListView', () => {
  it('groups own, shared and project tables with the six columns', async () => {
    mocks.projects = [
      project('pb', 'beta', { public: true }),
      project('pa', 'Alpha'),
      project('px', 'Broken'),
      project('parch', 'Old', { archived: true }),
    ];
    mocks.listTables.mockImplementation(async (opts) => {
      if (opts.shared) {
        return page([
          tableRow('s1', {
            title: 'Team roster',
            shared_with_me: true,
            permission: 'read',
            owner: { id: 2, name: 'Ana', email: 'ana@example.com' },
          }),
        ]);
      }
      if (!opts.projectId) {
        return page(
          [
            tableRow('u1', { title: 'Older', updated_at: '2026-10-01T00:00:00' }),
            tableRow('u2', { title: 'Newer', description: 'expenses', updated_at: '2026-10-05T00:00:00', row_count: 1, column_count: 1 }),
          ],
          'cursor-1',
        );
      }
      if (opts.projectId === 'pa') return page([tableRow('a1', { project_id: 'pa', title: 'Alpha leads' })]);
      if (opts.projectId === 'pb') {
        return page([tableRow('b1', { project_id: 'pb', mode: 'public', title: 'Beta leads' })]);
      }
      if (opts.projectId === 'parch') return page([tableRow('o1', { project_id: 'parch', title: 'Old list' })]);
      throw new Error('boom');
    });

    const { container } = renderView(null);
    await waitFor(() => expect(screen.getByText('Newer')).toBeTruthy());

    // One own page, one shared page, one fan-out fetch per project
    // (archived included) with the index's 200-row page.
    const fetched = mocks.listTables.mock.calls
      .filter(([opts]) => !opts.shared)
      .map(([opts]) => opts.projectId ?? null)
      .sort();
    expect(fetched).toEqual([null, 'pa', 'parch', 'pb', 'px']);
    expect(mocks.listTables.mock.calls.filter(([opts]) => opts.shared)).toEqual([
      [{ shared: true, limit: 50 }],
    ]);
    expect(mocks.listTables.mock.calls.find(([opts]) => opts.projectId === 'pa')?.[0].limit).toBe(200);

    // "Your tables" (count "+" while more pages exist), "Shared with you",
    // then projects by name; the failed project is left out.
    expect(groupHeadings(container)).toEqual([
      'Your tables2+',
      'Shared with you1',
      'Alpha1',
      'beta1',
      'OldArchived1',
    ]);
    expect(screen.getByText('Some project tables could not be loaded.')).toBeTruthy();

    // Newest-updated first inside a group; rows are links to the viewer.
    const own = screen.getByRole('region', { name: 'Your tables' });
    expect(rowTitles(own)).toEqual(['Newer', 'Older']);
    expect(within(own).getByText('Load more')).toBeTruthy();
    expect(own.querySelector('a.tables-row')?.getAttribute('href')).toBe('/tables/u2');

    // Column labels; "Mode" because a public table is on screen.
    expect([...container.querySelector('.tables-columns')!.children].map((c) => c.textContent)).toEqual(
      ['Title', 'Mode', 'Scope', 'Rows', 'Columns', 'Updated'],
    );

    // Cells: scope, rows, columns, updated; only the public row is badged.
    const newer = own.querySelector('a.tables-row') as HTMLElement;
    expect(newer.querySelector('.tables-row-description')?.textContent).toBe('expenses');
    expect(newer.querySelector('.tables-row-scope')?.textContent).toBe('Your table');
    expect(newer.querySelector('.tables-row-rows')?.textContent).toBe('1 row');
    expect(newer.querySelector('.tables-row-columns')?.textContent).toBe('1 column');
    expect(newer.querySelector('.tables-row-updated')?.textContent).not.toBe('');
    const sharedRow = screen.getByRole('region', { name: 'Shared with you' }).querySelector('a.tables-row')!;
    expect(sharedRow.querySelector('.tables-row-scope')?.textContent).toBe('Shared by Ana');
    expect(sharedRow.querySelector('.tables-row-scope')?.getAttribute('title')).toBe('Shared by Ana · can view');
    const alphaRow = screen.getByRole('region', { name: 'Alpha tables' }).querySelector('a.tables-row')!;
    expect(alphaRow.querySelector('.tables-row-scope')?.textContent).toBe('Alpha');
    expect(alphaRow.querySelector('.tables-row-rows')?.textContent).toBe('12 rows');
    expect(alphaRow.querySelector('.tables-row-columns')?.textContent).toBe('3 columns');
    expect([...container.querySelectorAll('.tables-row .doc-mode-badge')].map((b) => b.textContent)).toEqual([
      'Public',
    ]);

    // A project heading links to that project's filtered view.
    fireEvent.click(screen.getByTitle('Show only Alpha tables'));
    expect(screen.getByTestId('location').textContent).toBe('/tables?project=pa');
  });

  it('hides the Mode label when every row is private and an empty Shared group', async () => {
    mocks.listTables.mockImplementation(async (opts) =>
      opts.shared ? page([]) : page([tableRow('u1')]),
    );
    const { container } = renderView(null);
    await waitFor(() => expect(screen.getByText('Table u1')).toBeTruthy());
    expect(container.querySelector('.tables-columns')?.children[1]?.textContent).toBe('');
    expect(groupHeadings(container)).toEqual(['Your tables1']);
  });

  it('narrows to one project with ?project=<id>', async () => {
    mocks.projects = [project('pa', 'Alpha', { public: true }), project('pb', 'Beta')];
    mocks.listTables.mockImplementation(async (opts) =>
      opts.projectId === 'pa'
        ? page([
          tableRow('a1', { project_id: 'pa', updated_at: '2026-10-01T00:00:00' }),
          tableRow('a2', { project_id: 'pa', updated_at: '2026-10-03T00:00:00' }),
        ])
        : page([tableRow('other')]),
    );
    const { container } = renderView('pa');
    await waitFor(() => expect(screen.getByText('Table a1')).toBeTruthy());

    // Only the project's own stream: no shared list, no fan-out.
    expect(mocks.listTables.mock.calls.map(([opts]) => opts)).toEqual([{ projectId: 'pa', limit: 50 }]);
    expect(screen.getByRole('heading', { level: 2 }).textContent).toBe('AlphaPublic');
    expect(groupHeadings(container)).toEqual(['Tables2']);
    expect(rowTitles(container)).toEqual(['Table a2', 'Table a1']);
    expect(screen.queryByText('Table other')).toBeNull();

    fireEvent.click(screen.getByText('All tables'));
    expect(screen.getByTestId('location').textContent).toBe('/tables');
  });

  it('shows loading, then the empty state', async () => {
    let resolveOwn: (value: ListTablesResponse) => void = () => {};
    mocks.listTables.mockImplementation((opts) =>
      opts.shared ? Promise.resolve(page([])) : new Promise((resolve) => { resolveOwn = resolve; }),
    );
    renderView(null);
    expect(screen.getByText('Loading...')).toBeTruthy();
    resolveOwn(page([]));
    await waitFor(() => expect(screen.getByText('No tables yet.')).toBeTruthy());
    // A line on what a table is for, and a button that opens the dialog.
    expect(screen.getByText(EMPTY_HINT)).toBeTruthy();
    const create = screen.getByRole('button', { name: 'Create a table' });
    fireEvent.click(create);
    const dialog = screen.getByRole('dialog', { name: 'New Table' });
    expect((within(dialog).getByLabelText('Location') as HTMLSelectElement).value).toBe('');
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }));
    // Focus back on the button that opened it.
    expect(document.activeElement).toBe(create);
  });

  it('shows the filtered empty state', async () => {
    mocks.projects = [project('pa', 'Alpha')];
    mocks.listTables.mockResolvedValue(page([]));
    renderView('pa');
    await waitFor(() => expect(screen.getByText('No tables in this project yet.')).toBeTruthy());
    expect(screen.getByText(PROJECT_EMPTY_HINT)).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Create a table' }));
    const dialog = screen.getByRole('dialog', { name: 'New Table' });
    expect((within(dialog).getByLabelText('Location') as HTMLSelectElement).value).toBe('pa');
  });

  it("a public project's empty state does not send the user to ask Quest", async () => {
    mocks.projects = [project('pp', 'Open data', { public: true })];
    mocks.listTables.mockResolvedValue(page([]));
    renderView('pp');
    await waitFor(() => expect(screen.getByText('No tables in this project yet.')).toBeTruthy());
    expect(screen.getByText(PUBLIC_PROJECT_EMPTY_HINT)).toBeTruthy();
    expect(screen.queryByText(/Ask Quest/)).toBeNull();
    expect(screen.getByRole('button', { name: 'Create a table' })).toBeTruthy();
  });

  it('emptyHint picks the line by view and project', () => {
    expect(emptyHint(false, null)).toBe(EMPTY_HINT);
    expect(emptyHint(true, null)).toBe(PROJECT_EMPTY_HINT);
    expect(emptyHint(true, project('pa', 'Alpha'))).toBe(PROJECT_EMPTY_HINT);
    expect(emptyHint(true, project('pp', 'Open', { public: true }))).toBe(PUBLIC_PROJECT_EMPTY_HINT);
  });

  it('never reads as "No tables yet." while a project index failed', async () => {
    mocks.projects = [project('pa', 'Alpha')];
    mocks.listTables.mockImplementation(async (opts) => {
      if (opts.projectId) throw new Error('boom');
      return page([]);
    });
    renderView(null);
    await waitFor(() => expect(screen.getByText('Some project tables could not be loaded.')).toBeTruthy());
    expect(screen.queryByText('No tables yet.')).toBeNull();
    expect(screen.queryByText(EMPTY_HINT)).toBeNull();
    // The own group stays, saying it is empty.
    expect(within(screen.getByRole('region', { name: 'Your tables' })).getByText('No tables of your own yet.')).toBeTruthy();
  });

  it('reports a failed own list and a failed shared list', async () => {
    mocks.listTables.mockImplementation(async (opts) => {
      if (opts.shared) throw new Error('shared down');
      throw new Error('own down');
    });
    renderView(null);
    await waitFor(() => expect(screen.getByRole('region', { name: 'Shared with you' })).toBeTruthy());
    // Non-ApiClientError failures read as the hook's generic message.
    const alerts = screen.getAllByRole('alert').map((a) => a.textContent);
    expect(alerts).toEqual(['Failed to load tables', 'Failed to load tables']);
    expect(screen.queryByText('No tables yet.')).toBeNull();
    expect(screen.queryByRole('region', { name: 'Your tables' })).toBeNull();
  });

  it('opens its own New Table dialog without onNewTable', async () => {
    mocks.listTables.mockResolvedValue(page([]));
    mocks.projects = [project('p1', 'Sales')];
    renderView('p1');
    const button = screen.getByRole('button', { name: 'New Table' }) as HTMLButtonElement;
    expect(button.disabled).toBe(false);
    expect(screen.queryByRole('dialog')).toBeNull();
    fireEvent.click(button);
    const dialog = screen.getByRole('dialog', { name: 'New Table' });
    // Preselected to the filtered project.
    expect((within(dialog).getByLabelText('Location') as HTMLSelectElement).value).toBe('p1');
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }));
    expect(screen.queryByRole('dialog')).toBeNull();
    // Closed without creating: focus back on the button that opened it.
    expect(document.activeElement).toBe(button);
    await waitFor(() => expect(screen.getByText('No tables in this project yet.')).toBeTruthy());
  });

  it('calls onNewTable from an enabled New Table button', async () => {
    mocks.listTables.mockResolvedValue(page([]));
    const onNewTable = vi.fn();
    renderView(null, onNewTable);
    const button = screen.getByRole('button', { name: 'New Table' }) as HTMLButtonElement;
    expect(button.disabled).toBe(false);
    expect(button.getAttribute('title')).toBeNull();
    fireEvent.click(button);
    expect(onNewTable).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(screen.getByText('No tables yet.')).toBeTruthy());
    // The empty state's button goes to the host too.
    fireEvent.click(screen.getByRole('button', { name: 'Create a table' }));
    expect(onNewTable).toHaveBeenCalledTimes(2);
    expect(screen.queryByRole('dialog')).toBeNull();
  });

  it('shows an error under a generic heading for an unknown or foreign project', async () => {
    mocks.projects = [];
    mocks.listTables.mockRejectedValue(
      new ApiClientError('Project not found', 404, 'project_not_found'),
    );
    renderView('nope');
    await waitFor(() => expect(screen.getAllByRole('alert')).toHaveLength(1));
    expect(screen.getByRole('heading', { level: 2 }).textContent).toBe('Project');
    expect(screen.queryByText('No tables in this project yet.')).toBeNull();
    expect(screen.getByText('All tables')).toBeTruthy();
  });
});
