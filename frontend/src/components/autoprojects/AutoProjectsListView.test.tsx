// The AutoProjects list (/autoprojects and /autoprojects?archived=true): rows
// linking to each page with the working / "Needs you" / unread marks, Archive
// on an active AutoProject (disabled while a run is active), Unarchive and a
// confirmed Delete on an archived one, a failed action shown inline, the
// empty states and the switch between the two lists. useAutoProjects is
// mocked (it has its own tests).
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter, useLocation } from 'react-router-dom';
import { ApiClientError } from '../../api/request';
import type { AutoProjectListRow } from '../../api/types';
import type { UseAutoProjectsOptions } from '../../hooks/useAutoProjects';
import { AutoProjectsListView } from './AutoProjectsListView';

const mocks = vi.hoisted(() => ({
  rows: [] as AutoProjectListRow[],
  loading: false,
  error: null as string | null,
  refresh: vi.fn(),
  useAutoProjects: vi.fn(),
  archiveAutoProject: vi.fn(),
  unarchiveAutoProject: vi.fn(),
  deleteAutoProject: vi.fn(),
}));

vi.mock('../../hooks/useAutoProjects', () => ({
  useAutoProjects: (opts: UseAutoProjectsOptions) => {
    mocks.useAutoProjects(opts);
    return { autoprojects: mocks.rows, loading: mocks.loading, error: mocks.error, refresh: mocks.refresh };
  },
}));

vi.mock('../../api/autoprojectsApi', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../api/autoprojectsApi')>()),
  archiveAutoProject: mocks.archiveAutoProject,
  unarchiveAutoProject: mocks.unarchiveAutoProject,
  deleteAutoProject: mocks.deleteAutoProject,
}));

let nextId = 0;
function row(title: string, overrides: Partial<AutoProjectListRow> = {}): AutoProjectListRow {
  nextId += 1;
  return {
    id: `list-ap-${nextId}`,
    title,
    archived: false,
    last_activity_at: '2026-11-10T10:00:00',
    active_run: null,
    unread: false,
    event_seq: 1,
    unread_seq: 0,
    read_seq: 0,
    ...overrides,
  };
}

function LocationProbe() {
  const location = useLocation();
  return <div data-testid="location">{location.pathname + location.search}</div>;
}

function renderList(archived = false) {
  return render(
    <MemoryRouter initialEntries={[archived ? '/autoprojects?archived=true' : '/autoprojects']}>
      <AutoProjectsListView archived={archived} />
      <LocationProbe />
    </MemoryRouter>,
  );
}

function item(title: string): HTMLElement {
  return screen.getByRole('link', { name: new RegExp(title) }).closest('li') as HTMLElement;
}

beforeEach(() => {
  mocks.rows = [];
  mocks.loading = false;
  mocks.error = null;
  mocks.refresh.mockReset().mockResolvedValue(undefined);
  mocks.useAutoProjects.mockReset();
  mocks.archiveAutoProject.mockReset().mockResolvedValue({});
  mocks.unarchiveAutoProject.mockReset().mockResolvedValue({});
  mocks.deleteAutoProject.mockReset().mockResolvedValue({ success: true });
});

afterEach(cleanup);

describe('AutoProjectsListView, active list', () => {
  it('links each row to its page with its marks', () => {
    const working = row('Site build', { active_run: { id: 'r1', seq: 2, status: 'running' } });
    const waiting = row('Hiring plan', { active_run: { id: 'r2', seq: 3, status: 'waiting' } });
    const unread = row('Market study', { unread: true, unread_seq: 4, read_seq: 1 });
    mocks.rows = [working, waiting, unread];
    renderList();

    expect(mocks.useAutoProjects).toHaveBeenCalledWith({ archived: false });
    expect(screen.getByRole('heading', { name: 'AutoProjects' })).toBeTruthy();
    expect(screen.getByRole('link', { name: /Site build/ }).getAttribute('href')).toBe(`/autoprojects/${working.id}`);
    expect(within(item('Site build')).getByText('Working')).toBeTruthy();
    expect(within(item('Hiring plan')).getByText('Needs you')).toBeTruthy();
    expect(within(item('Market study')).getByRole('img', { name: 'Unread' })).toBeTruthy();
    expect(within(item('Site build')).queryByRole('img', { name: 'Unread' })).toBeNull();
  });

  it('archives an idle AutoProject and refreshes; Archive is disabled while a run is active', async () => {
    const idle = row('Idle one');
    const busy = row('Busy one', { active_run: { id: 'r1', seq: 1, status: 'running' } });
    mocks.rows = [idle, busy];
    renderList();

    const busyArchive = screen.getByRole('button', { name: 'Archive Busy one' }) as HTMLButtonElement;
    expect(busyArchive.disabled).toBe(true);

    fireEvent.click(screen.getByRole('button', { name: 'Archive Idle one' }));
    await waitFor(() => expect(mocks.archiveAutoProject).toHaveBeenCalledWith(idle.id));
    await waitFor(() => expect(mocks.refresh).toHaveBeenCalledTimes(1));
  });

  it('shows a refused archive inline', async () => {
    mocks.rows = [row('Idle one')];
    mocks.archiveAutoProject.mockRejectedValue(
      new ApiClientError('An update is still in progress.', 409, 'autoproject_run_active'),
    );
    renderList();
    fireEvent.click(screen.getByRole('button', { name: 'Archive Idle one' }));
    expect((await screen.findByRole('alert')).textContent).toContain('An update is still in progress.');
    expect(mocks.refresh).not.toHaveBeenCalled();
  });

  it('offers New AutoProject on the empty list and switches to the archive', () => {
    renderList();
    expect(screen.getByText('No AutoProjects yet.')).toBeTruthy();
    const newLinks = screen.getAllByRole('link', { name: 'New AutoProject' });
    expect(newLinks.every((l) => l.getAttribute('href') === '/autoprojects/new')).toBe(true);
    fireEvent.click(screen.getByRole('link', { name: 'Archived AutoProjects' }));
    expect(screen.getByTestId('location').textContent).toBe('/autoprojects?archived=true');
  });

  it('shows a failed first load with Retry', async () => {
    mocks.error = 'Failed to load AutoProjects';
    renderList();
    expect(screen.getByRole('alert').textContent).toBe('Failed to load AutoProjects');
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    await waitFor(() => expect(mocks.refresh).toHaveBeenCalledTimes(1));
  });
});

describe('AutoProjectsListView, archived list', () => {
  it('unarchives, and deletes only after the confirm', async () => {
    const old = row('Old study', { archived: true });
    mocks.rows = [old];
    renderList(true);
    expect(mocks.useAutoProjects).toHaveBeenCalledWith({ archived: true });
    expect(screen.getByRole('heading', { name: 'Archived AutoProjects' })).toBeTruthy();
    expect(screen.queryByRole('button', { name: /^Archive / })).toBeNull();
    expect(screen.queryByRole('link', { name: 'New AutoProject' })).toBeNull();

    fireEvent.click(screen.getByRole('button', { name: 'Unarchive Old study' }));
    await waitFor(() => expect(mocks.unarchiveAutoProject).toHaveBeenCalledWith(old.id));

    fireEvent.click(screen.getByRole('button', { name: 'Delete Old study' }));
    const dialog = screen.getByRole('dialog', { name: 'Delete AutoProject?' });
    expect(dialog.textContent).toContain('This cannot be undone.');
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }));
    expect(mocks.deleteAutoProject).not.toHaveBeenCalled();
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());

    fireEvent.click(screen.getByRole('button', { name: 'Delete Old study' }));
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Delete' }));
    await waitFor(() => expect(mocks.deleteAutoProject).toHaveBeenCalledWith(old.id));
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
  });

  it('keeps the dialog open with the error when the delete fails', async () => {
    mocks.rows = [row('Old study', { archived: true })];
    mocks.deleteAutoProject.mockRejectedValue(new ApiClientError('Server error', 500));
    renderList(true);
    fireEvent.click(screen.getByRole('button', { name: 'Delete Old study' }));
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Delete' }));
    expect((await within(screen.getByRole('dialog')).findByRole('alert')).textContent).toContain('Server error');
  });

  it('says so when nothing is archived', () => {
    renderList(true);
    expect(screen.getByText('No archived AutoProjects.')).toBeTruthy();
  });
});
