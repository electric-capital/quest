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
  useProjects: () => ({ projects: mocks.projects, projectsLoaded: true }),
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

function search(value: string) {
  fireEvent.change(screen.getByPlaceholderText('Search docs'), { target: { value } });
}

beforeEach(() => {
  mocks.fetchDocs.mockReset();
  mocks.createDoc.mockReset();
  mocks.global.clear();
  mocks.projects = [];
});

afterEach(() => {
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
      throw new Error('boom');
    });

    const { container } = renderView(null);
    await waitFor(() => expect(screen.getByText('My notes')).toBeTruthy());

    // One user-docs page plus one fetch per NON-archived project.
    const fetched = mocks.fetchDocs.mock.calls.map(([opts]) => opts.projectId ?? null).sort();
    expect(fetched).toEqual([null, 'pa', 'pb', 'px']);
    expect(mocks.fetchDocs.mock.calls.find(([opts]) => !opts.projectId)?.[0].limit).toBe(50);
    expect(mocks.fetchDocs.mock.calls.find(([opts]) => opts.projectId === 'pa')?.[0].limit).toBe(200);

    // "Your docs" first (count marked "+" while more pages exist), then
    // projects by name case-insensitively; the failed project is omitted.
    expect(groupHeadings(container)).toEqual(['Your docs1+', 'Alpha1', 'beta1']);
    expect(screen.getByText('Some project docs could not be loaded.')).toBeTruthy();
    expect(screen.getByText('Load more')).toBeTruthy();
    expect(screen.getAllByText('1.2 KB + 2 images')).toHaveLength(3);
    expect(container.querySelector('a.docs-row')?.getAttribute('href')).toBe('/docs/u1');

    // Search hides whole groups, matches descriptions, and reports no match.
    search('plan');
    expect(groupHeadings(container)).toEqual(['Alpha1', 'beta1']);
    search('grocer');
    expect(screen.getByText('My notes')).toBeTruthy();
    expect(groupHeadings(container)).toEqual(['Your docs1']);
    search('zzz');
    expect(screen.getByText("No docs match 'zzz'")).toBeTruthy();

    // doc_list_changed refreshes the user list and every project again.
    const before = mocks.fetchDocs.mock.calls.length;
    await act(async () => {
      mocks.global.forEach((cb) => cb({ type: 'doc_list_changed' }));
    });
    await waitFor(() => expect(mocks.fetchDocs.mock.calls.length).toBe(before + 4));
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
});
