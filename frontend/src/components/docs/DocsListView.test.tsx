import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { FetchDocsOptions } from '../../api/docsApi';
import type { CreateDocRequest, Doc, ListDocsResponse, Project } from '../../api/types';
import { DocsListView } from './DocsListView';

type GlobalListener = (event: { type: string; [key: string]: unknown }) => void;

const mocks = vi.hoisted(() => ({
  fetchDocs: vi.fn<(opts: FetchDocsOptions) => Promise<ListDocsResponse>>(),
  createDoc: vi.fn<(body: CreateDocRequest) => Promise<Doc>>(),
  projects: [] as Project[],
  projectsLoaded: true,
  enabledFeatures: ['docs', 'public_projects'] as string[],
  global: new Set<(event: { type: string; [key: string]: unknown }) => void>(),
}));

vi.mock('../../api/docsApi', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../api/docsApi')>()),
  fetchDocs: mocks.fetchDocs,
  createDoc: mocks.createDoc,
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

vi.mock('../../contexts/AuthContext', () => ({
  useAuth: () => ({ enabledFeatures: mocks.enabledFeatures }),
}));

function doc(id: string, overrides: Partial<Doc> = {}): Doc {
  return {
    id,
    owner_id: 1,
    project_id: null,
    title: `Doc ${id}`,
    description: '',
    mode: 'private',
    content_size: 1229,
    asset_count: 2,
    last_write_source: 'ui',
    created_at: '2026-10-01T00:00:00',
    updated_at: '2026-10-01T00:00:00',
    scope: overrides.project_id ? 'project' : 'user',
    shared: false,
    access: { can_rename: true, can_switch_mode: true, can_delete: true, write: 'free' },
    ...overrides,
  };
}

function project(id: string, name: string, overrides: Partial<Project> = {}): Project {
  return {
    id,
    user_id: 1,
    name,
    guide: '',
    public: false,
    archived: false,
    created_at: '2026-01-01T00:00:00',
    updated_at: null,
    conversation_count: 0,
    ...overrides,
  };
}

function page(docs: Doc[], nextCursor: string | null = null): ListDocsResponse {
  return { docs, has_more: nextCursor !== null, next_cursor: nextCursor };
}

function renderView(projectId: string | null) {
  return render(
    <MemoryRouter>
      <DocsListView projectId={projectId} />
    </MemoryRouter>,
  );
}

function groupHeadings(container: HTMLElement): (string | null)[] {
  return [...container.querySelectorAll('.docs-group-heading')].map((h) => h.textContent);
}

function modeColumnLabel(container: HTMLElement): string | null | undefined {
  return container.querySelector('.docs-columns')?.children[1]?.textContent;
}

function badgeLabels(container: HTMLElement): (string | null)[] {
  return [...container.querySelectorAll('.docs-row .doc-mode-badge')].map((b) => b.textContent);
}

function search(value: string) {
  fireEvent.change(screen.getByPlaceholderText('Search docs'), { target: { value } });
}

beforeEach(() => {
  mocks.fetchDocs.mockReset();
  mocks.createDoc.mockReset();
  mocks.global.clear();
  mocks.projects = [];
  mocks.projectsLoaded = true;
  mocks.enabledFeatures = ['docs', 'public_projects'];
});

afterEach(() => {
  vi.useRealTimers();
  cleanup();
});

describe('DocsListView', () => {
  it('groups user and project docs, searches, and warns about failed projects', async () => {
    mocks.projects = [
      project('pb', 'beta', { public: true }),
      project('pa', 'Alpha'),
      project('px', 'Broken'),
      project('parch', 'Old', { archived: true }),
    ];
    mocks.fetchDocs.mockImplementation(async (opts) => {
      if (!opts.projectId) {
        return page([doc('u1', { title: 'My notes', description: 'groceries' })], 'cursor-1');
      }
      if (opts.projectId === 'pa') {
        return page([doc('a1', { project_id: 'pa', title: 'Alpha plan' })]);
      }
      if (opts.projectId === 'pb') {
        return page([doc('b1', { project_id: 'pb', mode: 'public', title: 'Beta plan' })]);
      }
      if (opts.projectId === 'parch') {
        return page([doc('o1', { project_id: 'parch', title: 'Old notes' })]);
      }
      throw new Error('boom');
    });

    const { container } = renderView(null);
    await waitFor(() => expect(screen.getByText('My notes')).toBeTruthy());

    // One user-docs page plus one fetch per project, archived ones included
    // (archiving a project has no effect on its docs).
    const fetched = mocks.fetchDocs.mock.calls.map(([opts]) => opts.projectId ?? null).sort();
    expect(fetched).toEqual([null, 'pa', 'parch', 'pb', 'px']);
    expect(mocks.fetchDocs.mock.calls.find(([opts]) => !opts.projectId)?.[0].limit).toBe(50);
    expect(mocks.fetchDocs.mock.calls.find(([opts]) => opts.projectId === 'pa')?.[0].limit).toBe(200);

    // "Your docs" first (count marked "+" while more pages exist), then
    // projects by name case-insensitively; the failed project is omitted
    // and the archived one carries an "Archived" chip.
    expect(groupHeadings(container)).toEqual(['Your docs1+', 'Alpha1', 'beta1', 'OldArchived1']);
    expect(container.querySelectorAll('.docs-group-archived')).toHaveLength(1);
    expect(screen.getByText('Some project docs could not be loaded.')).toBeTruthy();
    expect(screen.getByText('Load more')).toBeTruthy();
    expect(screen.getAllByText('1.2 KB + 2 images')).toHaveLength(4);
    expect(container.querySelector('a.docs-row')?.getAttribute('href')).toBe('/docs/u1');

    // Search hides whole groups, matches descriptions, and reports no match.
    search('plan');
    expect(groupHeadings(container)).toEqual(['Alpha1', 'beta1']);
    search('grocer');
    expect(screen.getByText('My notes')).toBeTruthy();
    expect(groupHeadings(container)).toEqual(['Your docs1']);
    search('zzz');
    expect(screen.getByText("No docs match 'zzz'")).toBeTruthy();

    // doc_list_changed refreshes the user list per event, and every project
    // once per burst: the index re-fan-out waits for a 600 ms quiet period.
    vi.useFakeTimers();
    const before = mocks.fetchDocs.mock.calls.length;
    const projectFetches = () =>
      mocks.fetchDocs.mock.calls.slice(before).filter(([opts]) => opts.projectId).length;
    for (let i = 0; i < 3; i++) {
      await act(async () => {
        mocks.global.forEach((cb) => cb({ type: 'doc_list_changed' }));
        await vi.advanceTimersByTimeAsync(200);
      });
    }
    expect(mocks.fetchDocs.mock.calls.length).toBe(before + 3);
    expect(projectFetches()).toBe(0);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(399);
    });
    expect(projectFetches()).toBe(0);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1);
    });
    expect(projectFetches()).toBe(4);
    expect(mocks.fetchDocs.mock.calls.length).toBe(before + 3 + 4);
  });

  it('waits for the project list and the index before showing the empty state', async () => {
    // Cold load: the project list has not arrived yet, and the user's only
    // docs are project docs.
    mocks.projectsLoaded = false;
    mocks.projects = [];
    let resolveProject: (response: ListDocsResponse) => void = () => {};
    mocks.fetchDocs.mockImplementation((opts) => {
      if (!opts.projectId) return Promise.resolve(page([]));
      return new Promise((resolve) => {
        resolveProject = resolve;
      });
    });

    const { rerender } = renderView(null);
    await waitFor(() => expect(mocks.fetchDocs).toHaveBeenCalledTimes(1));
    await act(async () => {});
    // The user page is in, but nothing is fanned out over the empty
    // pre-load project list, and the view keeps loading.
    expect(mocks.fetchDocs.mock.calls[0][0].projectId ?? null).toBeNull();
    expect(screen.getByText('Loading...')).toBeTruthy();
    expect(screen.queryByText('No docs yet.')).toBeNull();

    mocks.projectsLoaded = true;
    mocks.projects = [project('pa', 'Alpha')];
    rerender(
      <MemoryRouter>
        <DocsListView projectId={null} />
      </MemoryRouter>,
    );
    await waitFor(() => expect(mocks.fetchDocs).toHaveBeenCalledTimes(2));
    expect(mocks.fetchDocs.mock.calls[1][0].projectId).toBe('pa');
    // The project fetch is still in flight: still loading, no empty state.
    expect(screen.getByText('Loading...')).toBeTruthy();
    expect(screen.queryByText('No docs yet.')).toBeNull();

    await act(async () => {
      resolveProject(page([doc('a1', { project_id: 'pa', title: 'Alpha plan' })]));
    });
    expect(screen.getByText('Alpha plan')).toBeTruthy();
    expect(screen.queryByText('No docs yet.')).toBeNull();
  });

  it('shows the empty state when there are no docs anywhere', async () => {
    mocks.projects = [project('pa', 'Alpha')];
    mocks.fetchDocs.mockResolvedValue(page([]));

    renderView(null);

    await waitFor(() => expect(screen.getByText('No docs yet.')).toBeTruthy());
    expect(screen.getByText('Ask Quest to create one, or use New Doc.')).toBeTruthy();
    expect(screen.queryByText('Some project docs could not be loaded.')).toBeNull();
  });

  it('lists one project when filtered and preselects it in New Doc', async () => {
    mocks.projects = [project('pb', 'beta', { public: true }), project('pa', 'Alpha')];
    mocks.fetchDocs.mockResolvedValue(page([doc('b1', { project_id: 'pb', mode: 'public' })]));

    const { container } = renderView('pb');
    await waitFor(() => expect(screen.getByText('Doc b1')).toBeTruthy());

    // Only the filtered project's page; no per-project fan-out.
    expect(mocks.fetchDocs).toHaveBeenCalledTimes(1);
    expect(mocks.fetchDocs.mock.calls[0][0]).toMatchObject({ projectId: 'pb', limit: 50 });
    expect(container.querySelector('.docs-list-title')?.textContent).toBe('betaPublic');
    expect(screen.getByText('All docs').closest('a')?.getAttribute('href')).toBe('/docs');

    fireEvent.click(screen.getByRole('button', { name: 'New Doc' }));
    const select = document.querySelector<HTMLSelectElement>('#new-doc-location-select');
    expect(select?.value).toBe('pb');
    // A project doc inherits the project's mode: read-only line, no radio.
    expect(document.querySelector('.new-doc-mode-inherited')?.textContent).toContain('Public');
    expect(document.querySelector('.new-doc-mode')).toBeNull();

    // Switching to "Your docs" brings the mode radio back.
    fireEvent.change(select!, { target: { value: '' } });
    expect(document.querySelector('.new-doc-mode')).not.toBeNull();

    fireEvent.change(document.querySelector('#new-doc-title-input')!, {
      target: { value: '  Hi ' },
    });
    mocks.createDoc.mockResolvedValue(doc('new1'));
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Create Doc' }));
    });
    expect(mocks.createDoc).toHaveBeenCalledWith({ title: 'Hi', mode: 'private' });
  });

  describe('with public projects closed for the user', () => {
    beforeEach(() => {
      mocks.enabledFeatures = ['docs'];
    });

    it('hides Private badges and the Mode column label when every row is private', async () => {
      mocks.fetchDocs.mockResolvedValue(page([doc('u1'), doc('u2')]));
      const { container } = renderView(null);
      await waitFor(() => expect(screen.getByText('Doc u1')).toBeTruthy());

      expect(badgeLabels(container)).toEqual([]);
      expect(modeColumnLabel(container)).toBe('');
      // The grid slot stays, so the other columns keep their place.
      expect(container.querySelectorAll('.docs-row-mode')).toHaveLength(2);
    });

    it('still badges a leftover public doc, and keeps the Mode label for it', async () => {
      mocks.fetchDocs.mockResolvedValue(page([doc('u1'), doc('u2', { mode: 'public' })]));
      const { container } = renderView(null);
      await waitFor(() => expect(screen.getByText('Doc u1')).toBeTruthy());

      expect(badgeLabels(container)).toEqual(['Public']);
      expect(modeColumnLabel(container)).toBe('Mode');
    });

    it('New Doc has no mode radio and sends no mode for a user doc', async () => {
      mocks.fetchDocs.mockResolvedValue(page([]));
      renderView(null);
      await waitFor(() => expect(screen.getByText('No docs yet.')).toBeTruthy());

      fireEvent.click(screen.getByRole('button', { name: 'New Doc' }));
      expect(document.querySelector('.new-doc-mode')).toBeNull();
      expect(screen.queryByRole('radio')).toBeNull();

      fireEvent.change(document.querySelector('#new-doc-title-input')!, {
        target: { value: 'Notes' },
      });
      mocks.createDoc.mockResolvedValue(doc('new1'));
      await act(async () => {
        fireEvent.click(screen.getByRole('button', { name: 'Create Doc' }));
      });
      expect(mocks.createDoc).toHaveBeenCalledWith({ title: 'Notes' });
    });

    it('New Doc drops the inherited-mode line for a private project, keeps it for a public one', async () => {
      // A leftover public project (normally hidden while the gate is closed)
      // still shows its public state.
      mocks.projects = [project('pa', 'Alpha'), project('pb', 'beta', { public: true })];
      mocks.fetchDocs.mockResolvedValue(page([]));
      renderView('pa');
      await waitFor(() => expect(mocks.fetchDocs).toHaveBeenCalled());

      fireEvent.click(screen.getByRole('button', { name: 'New Doc' }));
      const select = document.querySelector<HTMLSelectElement>('#new-doc-location-select')!;
      expect(select.value).toBe('pa');
      expect(document.querySelector('.new-doc-mode-inherited')).toBeNull();
      expect(document.querySelector('.new-doc-mode')).toBeNull();

      fireEvent.change(select, { target: { value: 'pb' } });
      expect(document.querySelector('.new-doc-mode-inherited')?.textContent).toContain('Public');

      fireEvent.change(document.querySelector('#new-doc-title-input')!, {
        target: { value: 'Plan' },
      });
      mocks.createDoc.mockResolvedValue(doc('new2', { project_id: 'pb', mode: 'public' }));
      fireEvent.change(select, { target: { value: 'pa' } });
      await act(async () => {
        fireEvent.click(screen.getByRole('button', { name: 'Create Doc' }));
      });
      expect(mocks.createDoc).toHaveBeenCalledWith({ title: 'Plan', project_id: 'pa' });
    });
  });
});
