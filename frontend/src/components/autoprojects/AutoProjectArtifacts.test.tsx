// The AutoProject page's "Docs and tables" card: the AutoProject's docs and
// tables as one newest-first list linking to their viewers, each kind only
// while its gate is open (nothing at all with both closed), the two lists
// asked for this AutoProject's stream, and New doc / New table opening the
// dialogs with the AutoProject as their fixed location (not on an archived
// AutoProject).
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import type { Doc, TableListRow } from '../../api/types';
import { AutoProjectArtifacts, mergeAutoProjectArtifacts } from './AutoProjectArtifacts';
import { makeAutoProject } from './autoprojectTestFixtures';

const mocks = vi.hoisted(() => ({
  features: [] as string[],
  docs: [] as unknown[],
  tables: [] as unknown[],
  docsArgs: [] as unknown[],
  tablesArgs: [] as unknown[],
  docsHasMore: false,
  loadMoreDocs: vi.fn(),
  docsError: null as string | null,
}));

vi.mock('../../contexts/AuthContext', () => ({
  useAuth: () => ({ enabledFeatures: mocks.features }),
}));
vi.mock('../../contexts/ProjectsContext', () => ({
  useProjects: () => ({ projects: [] }),
}));
vi.mock('../../hooks/useDocs', () => ({
  useDocs: (args: { enabled?: boolean }) => {
    mocks.docsArgs.push(args);
    return {
      docs: args.enabled ? mocks.docs : [],
      loading: false,
      loadingMore: false,
      error: mocks.docsError,
      hasMore: mocks.docsHasMore,
      loadMore: mocks.loadMoreDocs,
      refresh: vi.fn(),
    };
  },
}));
vi.mock('../../hooks/useTables', () => ({
  useTables: (args: { enabled?: boolean }) => {
    mocks.tablesArgs.push(args);
    return {
      tables: args.enabled ? mocks.tables : [],
      loading: false,
      loadingMore: false,
      error: null,
      hasMore: false,
      loadMore: vi.fn(),
      refresh: vi.fn(),
    };
  },
}));
vi.mock('../docs/NewDocModal', () => ({
  NewDocModal: (props: { isOpen: boolean; autoproject?: { id: string; title: string } | null }) =>
    props.isOpen ? <div data-testid="new-doc">{`${props.autoproject?.id}|${props.autoproject?.title}`}</div> : null,
}));
vi.mock('../tables/NewTableDialog', () => ({
  NewTableDialog: (props: { isOpen: boolean; autoproject?: { id: string; title: string } | null }) =>
    props.isOpen ? <div data-testid="new-table">{`${props.autoproject?.id}|${props.autoproject?.title}`}</div> : null,
}));

function doc(id: string, updatedAt: string, overrides: Partial<Doc> = {}): Doc {
  return {
    id, title: `Doc ${id}`, description: '', updated_at: updatedAt, autoproject_id: 'ap1', ...overrides,
  } as Doc;
}

function table(id: string, updatedAt: string, overrides: Partial<TableListRow> = {}): TableListRow {
  return {
    id, title: `Table ${id}`, description: '', updated_at: updatedAt, row_count: 3, autoproject_id: 'ap1',
    ...overrides,
  } as TableListRow;
}

function renderCard(overrides = {}) {
  return render(
    <MemoryRouter>
      <AutoProjectArtifacts autoproject={makeAutoProject({ title: 'Market study', ...overrides })} />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  mocks.features = ['docs', 'tables'];
  mocks.docs = [];
  mocks.tables = [];
  mocks.docsArgs = [];
  mocks.tablesArgs = [];
  mocks.docsHasMore = false;
  mocks.docsError = null;
  mocks.loadMoreDocs.mockReset();
});

afterEach(cleanup);

describe('mergeAutoProjectArtifacts', () => {
  it('orders docs and tables together, newest first', () => {
    const rows = mergeAutoProjectArtifacts(
      [doc('d1', '2026-11-10T10:00:00'), doc('d2', '2026-11-12T10:00:00')],
      [table('t1', '2026-11-11T10:00:00'), table('t2', '2026-11-12T10:00:00')],
    );
    expect(rows.map((r) => `${r.kind}:${r.id}`)).toEqual(['doc:d2', 'table:t2', 'table:t1', 'doc:d1']);
    expect(rows[1].rowCount).toBe(3);
    expect(rows[0].rowCount).toBeNull();
  });
});

describe('AutoProjectArtifacts', () => {
  it('lists the docs and tables of this AutoProject with links to their viewers', () => {
    mocks.docs = [doc('d1', '2026-11-10T10:00:00', { title: 'Findings', description: 'What we know' })];
    mocks.tables = [table('t1', '2026-11-11T10:00:00', { title: 'Leads', row_count: 1 })];
    renderCard();

    expect(screen.getByRole('heading', { name: 'Docs and tables' })).toBeTruthy();
    expect(screen.getByText('1 doc, 1 table')).toBeTruthy();
    const links = screen.getAllByRole('link');
    expect(links.map((a) => a.getAttribute('href'))).toEqual(['/tables/t1', '/docs/d1']);
    expect(within(links[0]).getByText('Leads')).toBeTruthy();
    expect(links[0].textContent).toContain('Table, 1 row');
    expect(within(links[1]).getByText('What we know')).toBeTruthy();
    // Both lists were asked for this AutoProject's stream.
    expect(mocks.docsArgs.at(-1)).toMatchObject({ autoprojectId: 'ap1', enabled: true });
    expect(mocks.tablesArgs.at(-1)).toMatchObject({ autoprojectId: 'ap1', enabled: true });
  });

  it('opens the create dialogs with the AutoProject as the location', () => {
    renderCard();
    expect(screen.getByText(/No docs or tables yet/)).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: 'New doc' }));
    expect(screen.getByTestId('new-doc').textContent).toBe('ap1|Market study');
    fireEvent.click(screen.getByRole('button', { name: 'New table' }));
    expect(screen.getByTestId('new-table').textContent).toBe('ap1|Market study');
  });

  it('shows only the kind whose gate is open', () => {
    mocks.features = ['docs'];
    mocks.docs = [doc('d1', '2026-11-10T10:00:00')];
    mocks.tables = [table('t1', '2026-11-11T10:00:00')];
    renderCard();

    expect(screen.getByRole('heading', { name: 'Docs' })).toBeTruthy();
    expect(screen.getAllByRole('link')).toHaveLength(1);
    expect(screen.queryByRole('button', { name: 'New table' })).toBeNull();
    expect(mocks.tablesArgs.at(-1)).toMatchObject({ enabled: false });
  });

  it('renders nothing with both gates closed', () => {
    mocks.features = ['autoprojects'];
    const { container } = renderCard();
    expect(container.firstChild).toBeNull();
    expect(mocks.docsArgs.at(-1)).toMatchObject({ enabled: false });
  });

  it('keeps the list and drops the buttons on an archived AutoProject', () => {
    mocks.docs = [doc('d1', '2026-11-10T10:00:00')];
    renderCard({ archived: true });

    expect(screen.getAllByRole('link')).toHaveLength(1);
    expect(screen.queryByRole('button', { name: 'New doc' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'New table' })).toBeNull();
  });

  it('pages with Show more and shows a load error', () => {
    mocks.docs = [doc('d1', '2026-11-10T10:00:00')];
    mocks.docsHasMore = true;
    mocks.docsError = 'Failed to load docs';
    renderCard();

    expect(screen.getByText('1+ doc')).toBeTruthy();
    expect(screen.getByRole('alert').textContent).toBe('Failed to load docs');
    fireEvent.click(screen.getByRole('button', { name: 'Show more' }));
    expect(mocks.loadMoreDocs).toHaveBeenCalledTimes(1);
  });
});
