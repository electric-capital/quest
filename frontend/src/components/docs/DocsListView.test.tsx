import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { FetchDocsOptions } from '../../api/docsApi';
import { ApiClientError } from '../../api/request';
import type { CreateDocRequest, Doc, ListDocsResponse, Project } from '../../api/types';
import { DocsListView } from './DocsListView';

type GlobalListener = (event: { type: string; [key: string]: unknown }) => void;

const mocks = vi.hoisted(() => ({
  fetchDocs: vi.fn<(opts: FetchDocsOptions) => Promise<ListDocsResponse>>(),
  createDoc: vi.fn<(body: CreateDocRequest) => Promise<Doc>>(),
  projects: [] as Project[],
  projectsLoaded: true,
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
    shared_with_me: false,
    permission: null,
    owner: null,
    last_write_user: null,
    access: { can_rename: true, can_switch_mode: false, can_delete: true, write: 'free', can_edit: true, can_share: true, can_delete_assets: true },
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
      if (opts.shared) return page([]);
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

    // One user-docs page, one shared-docs page, plus one fetch per project,
    // archived ones included (archiving a project has no effect on its docs).
    const fetched = mocks.fetchDocs.mock.calls
      .filter(([opts]) => !opts.shared)
      .map(([opts]) => opts.projectId ?? null)
      .sort();
    expect(fetched).toEqual([null, 'pa', 'parch', 'pb', 'px']);
    expect(mocks.fetchDocs.mock.calls.filter(([opts]) => opts.shared)).toEqual([
      [{ shared: true, limit: 50 }],
    ]);
    expect(
      mocks.fetchDocs.mock.calls.find(([opts]) => !opts.projectId && !opts.shared)?.[0].limit,
    ).toBe(50);
    expect(mocks.fetchDocs.mock.calls.find(([opts]) => opts.projectId === 'pa')?.[0].limit).toBe(200);

    // "Your docs" first (count marked "+" while more pages exist), no
    // "Shared with you" while nothing is shared, then projects by name
    // case-insensitively; the failed project is omitted and the archived
    // one carries an "Archived" chip.
    expect(groupHeadings(container)).toEqual(['Your docs1+', 'Alpha1', 'beta1', 'OldArchived1']);
    expect(container.querySelectorAll('.docs-group-archived')).toHaveLength(1);
    expect(screen.getByText('Some project docs could not be loaded.')).toBeTruthy();
    expect(screen.getByText('Load more')).toBeTruthy();
    expect(screen.getAllByText('1.2 KB + 2 images')).toHaveLength(4);
    expect(container.querySelector('a.docs-row')?.getAttribute('href')).toBe('/docs/u1');

    // Search hides whole groups, matches descriptions, and reports no match.
    // "Your docs" has more pages, so it stays (empty) for its "Load more".
    search('plan');
    expect(groupHeadings(container)).toEqual(['Your docs0', 'Alpha1', 'beta1']);
    expect(within(screen.getByRole('region', { name: 'Your docs' })).getByText('No matches loaded yet.')).toBeTruthy();
    expect(within(screen.getByRole('region', { name: 'Your docs' })).getByText('Load more')).toBeTruthy();
    search('grocer');
    expect(screen.getByText('My notes')).toBeTruthy();
    expect(groupHeadings(container)).toEqual(['Your docs1']);
    search('zzz');
    expect(screen.getByText("No docs match 'zzz'")).toBeTruthy();

    // A burst of doc_list_changed costs one refresh per list: the user and
    // shared lists 300 ms after the last event, every project 600 ms after
    // it (the index's own quiet period).
    vi.useFakeTimers();
    const before = mocks.fetchDocs.mock.calls.length;
    const sinceBurst = () => mocks.fetchDocs.mock.calls.slice(before).map(([opts]) => opts);
    const projectFetches = () => sinceBurst().filter((opts) => opts.projectId).length;
    const advance = async (ms: number) => {
      await act(async () => {
        await vi.advanceTimersByTimeAsync(ms);
      });
    };
    for (let i = 0; i < 3; i++) {
      await act(async () => {
        mocks.global.forEach((cb) => cb({ type: 'doc_list_changed' }));
        await vi.advanceTimersByTimeAsync(200);
      });
    }
    expect(sinceBurst()).toHaveLength(0);
    await advance(99);
    expect(sinceBurst()).toHaveLength(0);
    await advance(1);
    expect(sinceBurst()).toHaveLength(2);
    expect(sinceBurst().filter((opts) => opts.shared)).toHaveLength(1);
    await advance(299);
    expect(projectFetches()).toBe(0);
    await advance(1);
    expect(projectFetches()).toBe(4);
    expect(sinceBurst()).toHaveLength(2 + 4);
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
    await waitFor(() => expect(mocks.fetchDocs).toHaveBeenCalledTimes(2));
    await act(async () => {});
    // The user and shared pages are in, but nothing is fanned out over the
    // empty pre-load project list, and the view keeps loading.
    expect(mocks.fetchDocs.mock.calls.every(([opts]) => !opts.projectId)).toBe(true);
    expect(screen.getByText('Loading...')).toBeTruthy();
    expect(screen.queryByText('No docs yet.')).toBeNull();

    mocks.projectsLoaded = true;
    mocks.projects = [project('pa', 'Alpha')];
    rerender(
      <MemoryRouter>
        <DocsListView projectId={null} />
      </MemoryRouter>,
    );
    await waitFor(() => expect(mocks.fetchDocs).toHaveBeenCalledTimes(3));
    expect(mocks.fetchDocs.mock.calls[2][0].projectId).toBe('pa');
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

    // Only the filtered project's page; no per-project fan-out and no
    // shared list.
    expect(mocks.fetchDocs).toHaveBeenCalledTimes(1);
    expect(mocks.fetchDocs.mock.calls[0][0]).toMatchObject({ projectId: 'pb', limit: 50 });
    expect(container.querySelector('.docs-list-title')?.textContent).toBe('betaPublic');
    expect(screen.getByText('All docs').closest('a')?.getAttribute('href')).toBe('/docs');

    fireEvent.click(screen.getByRole('button', { name: 'New Doc' }));
    const select = document.querySelector<HTMLSelectElement>('#new-doc-location-select');
    expect(select?.value).toBe('pb');
    // A public project's doc inherits Public: a read-only line, no picker.
    expect(document.querySelector('.new-doc-mode-inherited')?.textContent).toBe(
      'Mode: Public — inherited from the project',
    );
    expect(screen.queryByRole('radio')).toBeNull();

    // "Your docs" is always private: no line, no picker.
    fireEvent.change(select!, { target: { value: '' } });
    expect(document.querySelector('.new-doc-mode-inherited')).toBeNull();
    expect(screen.queryByRole('radio')).toBeNull();

    fireEvent.change(document.querySelector('#new-doc-title-input')!, {
      target: { value: '  Hi ' },
    });
    mocks.createDoc.mockResolvedValue(doc('new1'));
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Create Doc' }));
    });
    // Never a `mode`: the server makes a user doc private.
    expect(mocks.createDoc).toHaveBeenCalledWith({ title: 'Hi' });
  });

  describe('Shared with you', () => {
    const readerAccess: Doc['access'] = {
      can_rename: false,
      can_switch_mode: false,
      can_delete: false,
      write: 'denied',
      can_edit: false,
      can_share: false,
      can_delete_assets: false,
    };

    function sharedDoc(id: string, overrides: Partial<Doc> = {}): Doc {
      return doc(id, {
        owner_id: 2,
        shared: true,
        shared_with_me: true,
        permission: 'read',
        owner: { id: 2, name: 'Ana', email: 'ana@x.test' },
        last_write_source: null,
        access: readerAccess,
        ...overrides,
      });
    }

    function region(name: string): HTMLElement {
      return screen.getByRole('region', { name });
    }

    it('lists shared user and project docs right after "Your docs", with the owner as scope', async () => {
      mocks.projects = [project('pa', 'Alpha')];
      mocks.fetchDocs.mockImplementation(async (opts) => {
        if (opts.shared) {
          return page([
            sharedDoc('s1', {
              title: 'Ana plan',
              permission: 'write',
              access: { ...readerAccess, write: 'free', can_edit: true },
            }),
            // Someone else's project doc: no project of the viewer's.
            sharedDoc('s2', {
              title: 'Bo roadmap',
              project_id: 'p-bo',
              scope: 'project',
              owner_id: 3,
              owner: { id: 3, name: '', email: 'bo@x.test' },
            }),
          ]);
        }
        if (opts.projectId === 'pa') return page([doc('a1', { project_id: 'pa', title: 'Alpha plan' })]);
        return page([doc('u1', { title: 'My notes' })]);
      });

      const { container } = renderView(null);
      await waitFor(() => expect(screen.getByText('Ana plan')).toBeTruthy());

      expect(groupHeadings(container)).toEqual(['Your docs1', 'Shared with you2', 'Alpha1']);
      const shared = region('Shared with you');
      const scopes = [...shared.querySelectorAll('.docs-row-scope')];
      expect(scopes.map((cell) => cell.textContent)).toEqual(['Shared by Ana', 'Shared by bo@x.test']);
      expect(scopes.map((cell) => cell.getAttribute('title'))).toEqual([
        'Shared by Ana · can edit',
        'Shared by bo@x.test · can view',
      ]);
      expect(within(shared).getByText('Bo roadmap').closest('a')?.getAttribute('href')).toBe('/docs/s2');
      // The viewer's own rows keep their scope and get no tooltip.
      const own = region('Your docs').querySelector('.docs-row-scope');
      expect(own?.textContent).toBe('Your doc');
      expect(own?.hasAttribute('title')).toBe(false);
      // The shared stream is never fanned out per project.
      expect(mocks.fetchDocs.mock.calls.some(([opts]) => opts.projectId === 'p-bo')).toBe(false);
    });

    it('pages the shared group by its own cursor', async () => {
      mocks.fetchDocs.mockImplementation(async (opts) => {
        if (opts.shared) {
          return opts.cursor
            ? page([sharedDoc('s2', { title: 'Second shared' })])
            : page([sharedDoc('s1', { title: 'First shared' })], 'shared-cursor');
        }
        return page([doc('u1', { title: 'My notes' })]);
      });

      const { container } = renderView(null);
      await waitFor(() => expect(screen.getByText('First shared')).toBeTruthy());
      expect(groupHeadings(container)).toEqual(['Your docs1', 'Shared with you1+']);
      // Only the shared group has more: one "Load more", inside it.
      expect(screen.getAllByText('Load more')).toHaveLength(1);
      expect(within(region('Your docs')).queryByText('Load more')).toBeNull();

      await act(async () => {
        fireEvent.click(within(region('Shared with you')).getByText('Load more'));
      });
      await waitFor(() => expect(screen.getByText('Second shared')).toBeTruthy());
      expect(mocks.fetchDocs).toHaveBeenLastCalledWith({ shared: true, limit: 50, cursor: 'shared-cursor' });
      expect(groupHeadings(container)).toEqual(['Your docs1', 'Shared with you2']);
      expect(screen.queryByText('Load more')).toBeNull();
    });

    it('searches shared docs (owner included), keeps paged groups reachable, and the no-match "Load more" pages every stream', async () => {
      mocks.fetchDocs.mockImplementation(async (opts) => {
        if (opts.shared) {
          return opts.cursor
            ? page([])
            : page([sharedDoc('s1', { title: 'Budget', description: 'Q4 numbers' })], 'shared-cursor');
        }
        return opts.cursor ? page([]) : page([doc('u1', { title: 'My notes' })], 'own-cursor');
      });

      const { container } = renderView(null);
      await waitFor(() => expect(screen.getByText('Budget')).toBeTruthy());

      // Both lists have more pages: a group with no loaded match stays, with
      // a "No matches loaded yet." line above its own "Load more".
      search('q4 numbers');
      expect(groupHeadings(container)).toEqual(['Your docs0', 'Shared with you1']);
      expect(within(region('Your docs')).getByText('No matches loaded yet.')).toBeTruthy();
      expect(within(region('Your docs')).getByText('Load more')).toBeTruthy();
      search('notes');
      expect(groupHeadings(container)).toEqual(['Your docs1', 'Shared with you0']);
      expect(within(region('Shared with you')).getByText('Load more')).toBeTruthy();
      // The owner's name and email match a shared row; the viewer's own rows
      // have no owner to match.
      search('ana@x.test');
      expect(within(region('Shared with you')).getByText('Budget')).toBeTruthy();
      search('Ana');
      expect(within(region('Shared with you')).getByText('Budget')).toBeTruthy();
      expect(within(region('Your docs')).queryByText('My notes')).toBeNull();

      search('zzz');
      expect(screen.getByText("No docs match 'zzz'")).toBeTruthy();
      const before = mocks.fetchDocs.mock.calls.length;
      await act(async () => {
        fireEvent.click(screen.getByText('Load more'));
      });
      const paged = mocks.fetchDocs.mock.calls.slice(before).map(([opts]) => opts);
      expect(paged).toEqual(
        expect.arrayContaining([
          { projectId: null, limit: 50, cursor: 'own-cursor' },
          { shared: true, limit: 50, cursor: 'shared-cursor' },
        ]),
      );
      expect(paged).toHaveLength(2);
    });

    it('waits for the shared list, so a user with only shared docs never sees "No docs yet."', async () => {
      let resolveShared: (response: ListDocsResponse) => void = () => {};
      mocks.fetchDocs.mockImplementation((opts) => {
        if (opts.shared) {
          return new Promise((resolve) => {
            resolveShared = resolve;
          });
        }
        return Promise.resolve(page([]));
      });

      const { container } = renderView(null);
      await waitFor(() => expect(mocks.fetchDocs).toHaveBeenCalledTimes(2));
      await act(async () => {});
      expect(screen.getByText('Loading...')).toBeTruthy();
      expect(screen.queryByText('No docs yet.')).toBeNull();

      await act(async () => {
        resolveShared(page([sharedDoc('s1', { title: 'Team handbook' })]));
      });
      expect(screen.getByText('Team handbook')).toBeTruthy();
      expect(screen.queryByText('No docs yet.')).toBeNull();
      // "Your docs" stays, saying it is empty.
      expect(groupHeadings(container)).toEqual(['Your docs0', 'Shared with you1']);
      expect(screen.getByText('No docs of your own yet.')).toBeTruthy();
    });

    it('shows a failed shared list in its group instead of the empty state', async () => {
      mocks.fetchDocs.mockImplementation(async (opts) => {
        if (opts.shared) throw new ApiClientError('Shared docs are unavailable', 500);
        return page([]);
      });

      const { container } = renderView(null);
      await waitFor(() => expect(screen.getByText('Shared docs are unavailable')).toBeTruthy());
      expect(screen.queryByText('No docs yet.')).toBeNull();
      expect(groupHeadings(container)).toEqual(['Your docs0', 'Shared with you0']);
      expect(within(region('Shared with you')).getByRole('alert').textContent).toBe(
        'Shared docs are unavailable',
      );
    });
  });

  describe('mode badges', () => {
    it('badges no private row and leaves the Mode column label out when every row is private', async () => {
      mocks.projects = [project('pa', 'Alpha')];
      mocks.fetchDocs.mockImplementation(async (opts) =>
        opts.shared
          ? page([])
          : opts.projectId
            ? page([doc('a1', { project_id: 'pa', title: 'Alpha plan' })])
            : page([doc('u1'), doc('u2')]),
      );
      const { container } = renderView(null);
      await waitFor(() => expect(screen.getByText('Alpha plan')).toBeTruthy());

      expect(badgeLabels(container)).toEqual([]);
      expect(modeColumnLabel(container)).toBe('');
      // The grid slot stays, so the other columns keep their place.
      expect(container.querySelectorAll('.docs-row-mode')).toHaveLength(3);
    });

    it('badges a public row and shows the Mode label only while one is visible', async () => {
      mocks.projects = [project('pb', 'beta', { public: true })];
      mocks.fetchDocs.mockImplementation(async (opts) =>
        opts.shared
          ? page([])
          : opts.projectId
            ? page([doc('b1', { project_id: 'pb', mode: 'public', title: 'Beta plan' })])
            : page([doc('u1', { title: 'My notes' })]),
      );
      const { container } = renderView(null);
      await waitFor(() => expect(screen.getByText('Beta plan')).toBeTruthy());

      expect(badgeLabels(container)).toEqual(['Public']);
      expect(modeColumnLabel(container)).toBe('Mode');

      // A search that hides the public row drops the label with it.
      search('notes');
      expect(badgeLabels(container)).toEqual([]);
      expect(modeColumnLabel(container)).toBe('');
    });
  });

  describe('New Doc', () => {
    it('has no mode picker and sends no mode for a user doc', async () => {
      mocks.fetchDocs.mockResolvedValue(page([]));
      renderView(null);
      await waitFor(() => expect(screen.getByText('No docs yet.')).toBeTruthy());

      fireEvent.click(screen.getByRole('button', { name: 'New Doc' }));
      expect(screen.queryByRole('radio')).toBeNull();
      expect(screen.queryByText(/Mode/)).toBeNull();

      fireEvent.change(document.querySelector('#new-doc-title-input')!, {
        target: { value: 'Notes' },
      });
      mocks.createDoc.mockResolvedValue(doc('new1'));
      await act(async () => {
        fireEvent.click(screen.getByRole('button', { name: 'Create Doc' }));
      });
      expect(mocks.createDoc).toHaveBeenCalledWith({ title: 'Notes' });
    });

    it('shows the inherited-mode line only for a public project, and sends no mode', async () => {
      mocks.projects = [project('pa', 'Alpha'), project('pb', 'beta', { public: true })];
      mocks.fetchDocs.mockResolvedValue(page([]));
      renderView('pa');
      await waitFor(() => expect(mocks.fetchDocs).toHaveBeenCalled());

      fireEvent.click(screen.getByRole('button', { name: 'New Doc' }));
      const select = document.querySelector<HTMLSelectElement>('#new-doc-location-select')!;
      expect(select.value).toBe('pa');
      expect(document.querySelector('.new-doc-mode-inherited')).toBeNull();

      fireEvent.change(select, { target: { value: 'pb' } });
      expect(document.querySelector('.new-doc-mode-inherited')?.textContent).toBe(
        'Mode: Public — inherited from the project',
      );
      expect(screen.queryByRole('radio')).toBeNull();

      fireEvent.change(document.querySelector('#new-doc-title-input')!, {
        target: { value: 'Plan' },
      });
      mocks.createDoc.mockResolvedValue(doc('new2', { project_id: 'pb', mode: 'public' }));
      await act(async () => {
        fireEvent.click(screen.getByRole('button', { name: 'Create Doc' }));
      });
      expect(mocks.createDoc).toHaveBeenCalledWith({ title: 'Plan', project_id: 'pb' });

      // A private project's doc gets the same request shape, no line.
      fireEvent.click(screen.getByRole('button', { name: 'New Doc' }));
      fireEvent.change(document.querySelector('#new-doc-title-input')!, {
        target: { value: 'Other' },
      });
      mocks.createDoc.mockResolvedValue(doc('new3', { project_id: 'pa' }));
      await act(async () => {
        fireEvent.click(screen.getByRole('button', { name: 'Create Doc' }));
      });
      expect(mocks.createDoc).toHaveBeenLastCalledWith({ title: 'Other', project_id: 'pa' });
    });
  });
});
