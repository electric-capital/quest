// The brief's History: the version list with source and time, one version
// rendered or as a word diff against the current brief, Restore through a
// confirm that sends the version current when it opened, and the stale /
// gone refusals.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { ApiClientError } from '../../api/request';
import type { AutoProject, AutoProjectBriefVersion } from '../../api/types';
import { AutoProjectBriefHistory } from './AutoProjectBriefHistory';
import { makeAutoProject } from './autoprojectTestFixtures';

const mocks = vi.hoisted(() => ({
  listAutoProjectBriefVersions: vi.fn(),
  getAutoProjectBriefVersion: vi.fn(),
  restoreAutoProjectBriefVersion: vi.fn(),
}));

vi.mock('../../api/autoprojectsApi', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../api/autoprojectsApi')>()),
  listAutoProjectBriefVersions: mocks.listAutoProjectBriefVersions,
  getAutoProjectBriefVersion: mocks.getAutoProjectBriefVersion,
  restoreAutoProjectBriefVersion: mocks.restoreAutoProjectBriefVersion,
}));

const CURRENT = makeAutoProject({ brief: 'Ship the final report today', brief_version: 3, brief_source: 'ui' });

const VERSIONS: AutoProjectBriefVersion[] = [
  { version: 3, source: 'ui', run_seq: null, created_at: '2026-11-10T12:00:00', excerpt: 'Ship the final report today' },
  { version: 2, source: 'run:run-2', run_seq: 2, created_at: '2026-11-10T11:00:00', excerpt: 'Ship the draft report' },
  { version: 1, source: 'restore:0', run_seq: null, created_at: '2026-11-10T10:00:00', excerpt: '' },
];

function renderHistory(autoproject: AutoProject = CURRENT) {
  const props = { onRestored: vi.fn(), onStale: vi.fn() };
  const utils = render(<AutoProjectBriefHistory autoproject={autoproject} {...props} />);
  return {
    ...utils,
    ...props,
    rerenderWith: (next: AutoProject) => utils.rerender(<AutoProjectBriefHistory autoproject={next} {...props} />),
  };
}

async function openVersion(version: number) {
  fireEvent.click(await screen.findByRole('button', { name: new RegExp(`^Version ${version}`) }));
}

beforeEach(() => {
  mocks.listAutoProjectBriefVersions.mockReset().mockResolvedValue({ versions: VERSIONS });
  mocks.getAutoProjectBriefVersion.mockReset().mockImplementation(async (_id: string, version: number) => ({
    ...VERSIONS.find((v) => v.version === version)!,
    content: version === 2 ? 'Ship the draft report' : '',
  }));
  mocks.restoreAutoProjectBriefVersion.mockReset();
});

afterEach(cleanup);

describe('AutoProjectBriefHistory list', () => {
  it('lists the versions newest first with their source, time and excerpt', async () => {
    renderHistory();
    const list = await screen.findByRole('list', { name: 'Versions' });
    const rows = within(list).getAllByRole('listitem');
    expect(rows.map((r) => within(r).getByRole('button').textContent)).toEqual([
      'Version 3Current',
      'Version 2',
      'Version 1',
    ]);
    expect(rows[0].textContent).toMatch(/Edited by you · /);
    expect(rows[1].textContent).toMatch(/Update 2 · .*Ship the draft report/);
    expect(rows[2].textContent).toMatch(/Restored from version 0 · /);
    expect(screen.getByText('The last 50 versions of the brief are kept.')).toBeTruthy();
    expect(mocks.listAutoProjectBriefVersions).toHaveBeenCalledWith('ap1');
  });

  it('says when there are no versions yet', async () => {
    mocks.listAutoProjectBriefVersions.mockResolvedValue({ versions: [] });
    renderHistory();
    expect(await screen.findByText('No versions yet.')).toBeTruthy();
  });

  it('refetches the list only when the brief version moves', async () => {
    const { rerenderWith } = renderHistory();
    await screen.findByRole('list', { name: 'Versions' });
    rerenderWith({ ...CURRENT, title: 'Renamed' });
    expect(mocks.listAutoProjectBriefVersions).toHaveBeenCalledTimes(1);
    rerenderWith({ ...CURRENT, brief_version: 4 });
    await waitFor(() => expect(mocks.listAutoProjectBriefVersions).toHaveBeenCalledTimes(2));
  });

  it('shows a failed list load with Retry', async () => {
    mocks.listAutoProjectBriefVersions.mockRejectedValueOnce(new ApiClientError('Server unavailable', 503));
    renderHistory();
    expect(await screen.findByText('Server unavailable')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    expect(await screen.findByRole('list', { name: 'Versions' })).toBeTruthy();
  });
});

describe('AutoProjectBriefHistory version', () => {
  it('shows a version, a word diff against the current brief, and goes back on Escape', async () => {
    renderHistory();
    await openVersion(2);
    const detail = await screen.findByRole('article', { name: 'Version 2' });
    expect(await within(detail).findByText('Ship the draft report')).toBeTruthy();
    expect(mocks.getAutoProjectBriefVersion).toHaveBeenCalledWith('ap1', 2);
    expect(document.activeElement).toBe(screen.getByRole('button', { name: 'All versions' }));

    fireEvent.click(screen.getByRole('button', { name: 'Show changes' }));
    const diff = screen.getByRole('region', { name: 'Changes since this version' });
    expect(Array.from(diff.querySelectorAll('del')).map((d) => d.textContent)).toEqual(['draft']);
    expect(Array.from(diff.querySelectorAll('ins')).map((d) => d.textContent).join(' ')).toContain('final');
    expect(Array.from(diff.querySelectorAll('ins')).map((d) => d.textContent).join(' ')).toContain('today');

    fireEvent.keyDown(detail, { key: 'Escape' });
    expect(await screen.findByRole('list', { name: 'Versions' })).toBeTruthy();
    expect(document.activeElement?.textContent).toBe('Version 2');
  });

  it('shows the current version from the brief itself, without Restore', async () => {
    renderHistory();
    await openVersion(3);
    const detail = await screen.findByRole('article', { name: 'Version 3' });
    expect(within(detail).getByText('Ship the final report today')).toBeTruthy();
    expect(mocks.getAutoProjectBriefVersion).not.toHaveBeenCalled();
    expect(screen.queryByRole('button', { name: 'Restore' })).toBeNull();
  });

  it('says a version is empty', async () => {
    renderHistory();
    await openVersion(1);
    expect(await screen.findByText('This version is empty.')).toBeTruthy();
  });

  it('reports a version that is gone and reloads the list', async () => {
    mocks.getAutoProjectBriefVersion.mockRejectedValue(
      new ApiClientError('Not found', 404, 'brief_version_not_found'),
    );
    renderHistory();
    await openVersion(2);
    expect(await screen.findByText('That version is no longer available.')).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Retry' })).toBeNull();
    await waitFor(() => expect(mocks.listAutoProjectBriefVersions).toHaveBeenCalledTimes(2));
  });
});

describe('AutoProjectBriefHistory restore', () => {
  async function openRestore() {
    renderHistory();
    await openVersion(2);
    fireEvent.click(await screen.findByRole('button', { name: 'Restore' }));
    return screen.getByRole('dialog', { name: 'Restore version 2?' });
  }

  it('restores against the version current when the confirm opened', async () => {
    mocks.restoreAutoProjectBriefVersion.mockResolvedValue({ brief: 'Ship the draft report', brief_version: 4 });
    const props = { onRestored: vi.fn(), onStale: vi.fn() };
    render(<AutoProjectBriefHistory autoproject={CURRENT} {...props} />);
    await openVersion(2);
    fireEvent.click(await screen.findByRole('button', { name: 'Restore' }));
    const dialog = screen.getByRole('dialog', { name: 'Restore version 2?' });
    expect(dialog.textContent).toContain('The current brief (version 3) is kept in History');

    fireEvent.click(within(dialog).getByRole('button', { name: 'Restore' }));
    expect(mocks.restoreAutoProjectBriefVersion).toHaveBeenCalledWith('ap1', 2, 3);
    await waitFor(() =>
      expect(props.onRestored).toHaveBeenCalledWith({ brief: 'Ship the draft report', brief_version: 4 }),
    );
    expect(screen.getByText('Version 2 was restored as version 4.')).toBeTruthy();
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(screen.getByRole('list', { name: 'Versions' })).toBeTruthy();
  });

  it('closes on a 409 stale_brief with a notice, refreshes the page and reloads the list', async () => {
    mocks.restoreAutoProjectBriefVersion.mockRejectedValue(
      new ApiClientError('stale', 409, 'stale_brief', undefined, { detail: { error: 'stale_brief' } }),
    );
    const props = { onRestored: vi.fn(), onStale: vi.fn() };
    render(<AutoProjectBriefHistory autoproject={CURRENT} {...props} />);
    await openVersion(2);
    fireEvent.click(await screen.findByRole('button', { name: 'Restore' }));
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Restore' }));

    expect(await screen.findByText(/The brief changed meanwhile/)).toBeTruthy();
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(props.onStale).toHaveBeenCalledTimes(1);
    expect(props.onRestored).not.toHaveBeenCalled();
    await waitFor(() => expect(mocks.listAutoProjectBriefVersions).toHaveBeenCalledTimes(2));
  });

  it('keeps other failures inline in the dialog', async () => {
    mocks.restoreAutoProjectBriefVersion.mockRejectedValue(new ApiClientError('Server unavailable', 503));
    const dialog = await openRestore();
    fireEvent.click(within(dialog).getByRole('button', { name: 'Restore' }));
    expect((await within(dialog).findByRole('alert')).textContent).toBe('Server unavailable');
    expect(screen.getByRole('dialog')).toBeTruthy();
  });

  it('mentions an active update merging the restore', async () => {
    renderHistory({ ...CURRENT, active_run: { id: 'run-4', seq: 4, status: 'running' } });
    await openVersion(2);
    fireEvent.click(await screen.findByRole('button', { name: 'Restore' }));
    expect(screen.getByRole('dialog').textContent).toContain('it will merge this');
  });
});
