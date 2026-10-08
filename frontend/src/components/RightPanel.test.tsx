// RightPanel card set per screen (standalone / project conversation / drilled
// home / nothing), the Copy / Move row actions between Chat Files and Project
// Files (useWorkspaceCopy) incl. the 409 overwrite prompt and the notices,
// and the persisted vertical split.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import type { FileEntry } from '../api/types';
import type { FileSource } from '../api/fileApi';
import type { FileBrowserNotice, FileRowAction, FileRowContext } from './FileBrowser';
import {
  RightPanel, getSplit3StorageKey, getSplitStorageKey, moveSplitBoundary, normalizeShares,
} from './RightPanel';

const mocks = vi.hoisted(() => ({
  copyFileToProject: vi.fn(),
  copyFileFromProject: vi.fn(),
  drilledProjectId: null as string | null,
  showHidden: false,
}));

// jsdom has no PointerEvent; the dividers drag with pointer events.
if (typeof window.PointerEvent === 'undefined') {
  class PointerEventPolyfill extends MouseEvent {
    pointerId: number;
    pointerType: string;
    constructor(type: string, init: PointerEventInit = {}) {
      super(type, init);
      this.pointerId = init.pointerId ?? 1;
      this.pointerType = init.pointerType ?? 'mouse';
    }
  }
  (window as unknown as { PointerEvent: unknown }).PointerEvent = PointerEventPolyfill;
}

vi.mock('../api/fileApi', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/fileApi')>();
  return {
    ...actual,
    copyFileToProject: mocks.copyFileToProject,
    copyFileFromProject: mocks.copyFileFromProject,
  };
});

vi.mock('../contexts/ProjectsContext', () => ({
  useProjects: () => ({ drilledProjectId: mocks.drilledProjectId }),
}));

vi.mock('./ProjectTables', () => ({
  ProjectTables: ({ projectId }: { projectId: string }) => (
    <div data-testid="tables">tables:{projectId}</div>
  ),
}));

const fileRow = (name: string, type: 'file' | 'folder' = 'file'): FileEntry => ({
  name, type, size: type === 'file' ? 10 : null, lastModified: '2026-10-01T00:00:00Z',
});

// Stub card: title by source, one row per card ("a.csv" / folder "data")
// whose host actions render as buttons, and the host notice.
vi.mock('./FileBrowser', () => ({
  FileBrowser: (props: {
    source: FileSource | null;
    rowActions?: (row: FileRowContext) => FileRowAction[];
    notice?: FileBrowserNotice | null;
    onDismissNotice?: () => void;
  }) => {
    const { source } = props;
    const title = !source ? 'Files' : source.kind === 'project' ? 'Project Files' : 'Chat Files';
    const rows: FileEntry[] = [
      fileRow('a.csv'), fileRow('data', 'folder'), fileRow('pasted', 'folder'), fileRow('.responses', 'folder'),
    ];
    return (
      <section data-testid={`card-${title}`} data-source={source ? `${source.kind}:${source.id}` : 'none'}>
        <h3>{title}</h3>
        {source && props.rowActions && rows.flatMap((entry) =>
          props.rowActions!({ entry, path: `/${entry.name}`, source, showHidden: mocks.showHidden }).map((a) => (
            <button key={`${entry.name}-${a.key}`} disabled={a.disabled} onClick={a.onSelect}>
              {a.label} {entry.name}
            </button>
          )))}
        {props.notice && (
          <div data-testid={`notice-${title}`} data-tone={props.notice.tone}>
            {props.notice.message}
            <button onClick={props.onDismissNotice}>Dismiss</button>
          </div>
        )}
      </section>
    );
  },
}));

function destinationExists() {
  // Built lazily so the real FileApiError class (via importOriginal) is used.
  return import('../api/fileApi').then(
    ({ FileApiError }) => new FileApiError('Destination exists', 409, 'destination_exists'),
  );
}

const OK = { type: 'file', path: '/a.csv', files_copied: 1, skipped: 0, moved: false };

beforeEach(() => {
  mocks.drilledProjectId = null;
  mocks.showHidden = false;
  mocks.copyFileToProject.mockReset();
  mocks.copyFileFromProject.mockReset();
  localStorage.clear();
});

afterEach(() => {
  cleanup();
});

const cardTitles = () => screen.getAllByRole('heading', { level: 3 }).map((h) => h.textContent);

describe('card set', () => {
  it('standalone conversation: Chat Files alone', () => {
    render(<RightPanel conversationId="c1" projectId={null} />);
    expect(cardTitles()).toEqual(['Chat Files']);
    expect(screen.getByTestId('card-Chat Files').dataset.source).toBe('conversation:c1');
    expect(screen.queryByTestId('tables')).toBeNull();
    expect(screen.queryByTestId('right-panel-divider-0')).toBeNull();
  });

  it('standalone conversation ignores a drilled project', () => {
    mocks.drilledProjectId = 'p9';
    render(<RightPanel conversationId="c1" projectId={null} />);
    expect(cardTitles()).toEqual(['Chat Files']);
  });

  it('project conversation: Chat Files, Project Files and Tables with two dividers', () => {
    render(<RightPanel conversationId="c1" projectId="p1" />);
    expect(cardTitles()).toEqual(['Chat Files', 'Project Files']);
    expect(screen.getByTestId('card-Chat Files').dataset.source).toBe('conversation:c1');
    expect(screen.getByTestId('card-Project Files').dataset.source).toBe('project:p1');
    expect(screen.getByTestId('tables').textContent).toBe('tables:p1');
    expect(screen.getByTestId('right-panel-divider-0')).toBeTruthy();
    expect(screen.getByTestId('right-panel-divider-1')).toBeTruthy();
  });

  it('drilled home composer: Project Files and Tables over the project routes', () => {
    mocks.drilledProjectId = 'p2';
    render(<RightPanel conversationId={null} projectId={null} />);
    expect(cardTitles()).toEqual(['Project Files']);
    expect(screen.getByTestId('card-Project Files').dataset.source).toBe('project:p2');
    expect(screen.getByTestId('tables').textContent).toBe('tables:p2');
    expect(screen.getByTestId('right-panel-divider-0')).toBeTruthy();
    expect(screen.queryByTestId('right-panel-divider-1')).toBeNull();
  });

  it('nothing on screen: the empty FileBrowser', () => {
    render(<RightPanel conversationId={null} projectId={null} />);
    expect(cardTitles()).toEqual(['Files']);
    expect(screen.getByTestId('card-Files').dataset.source).toBe('none');
  });
});

describe('Copy / Move row actions', () => {
  it('are absent in a standalone chat and on the drilled home screen', () => {
    const { unmount } = render(<RightPanel conversationId="c1" projectId={null} />);
    expect(screen.queryByRole('button', { name: /to project|to chat/ })).toBeNull();
    unmount();
    mocks.drilledProjectId = 'p2';
    render(<RightPanel conversationId={null} projectId={null} />);
    expect(screen.queryByRole('button', { name: /to project|to chat/ })).toBeNull();
  });

  it('appear on both cards of a project conversation', () => {
    render(<RightPanel conversationId="c1" projectId="p1" />);
    const chat = within(screen.getByTestId('card-Chat Files'));
    const project = within(screen.getByTestId('card-Project Files'));
    expect(chat.getByRole('button', { name: 'Copy to project a.csv' })).toBeTruthy();
    expect(chat.getByRole('button', { name: 'Move to project data' })).toBeTruthy();
    expect(chat.queryByRole('button', { name: /to chat/ })).toBeNull();
    expect(project.getByRole('button', { name: 'Copy to chat a.csv' })).toBeTruthy();
    expect(project.getByRole('button', { name: 'Move to chat data' })).toBeTruthy();
    expect(project.queryByRole('button', { name: /to project/ })).toBeNull();
  });

  it.each([
    ['Copy to project a.csv', 'to', { path: '/a.csv', move: false, includeHidden: false }],
    ['Move to project data', 'to', { path: '/data', move: true, includeHidden: false }],
    ['Copy to chat a.csv', 'from', { path: '/a.csv', move: false, includeHidden: false }],
    ['Move to chat a.csv', 'from', { path: '/a.csv', move: true, includeHidden: false }],
  ] as const)('%s calls the right copy route', async (label, direction, args) => {
    mocks.copyFileToProject.mockResolvedValue({ ...OK, moved: args.move });
    mocks.copyFileFromProject.mockResolvedValue({ ...OK, moved: args.move });
    render(<RightPanel conversationId="c1" projectId="p1" />);
    fireEvent.click(screen.getByRole('button', { name: label }));
    const called = direction === 'to' ? mocks.copyFileToProject : mocks.copyFileFromProject;
    const other = direction === 'to' ? mocks.copyFileFromProject : mocks.copyFileToProject;
    await waitFor(() => expect(called).toHaveBeenCalledWith('c1', args));
    expect(other).not.toHaveBeenCalled();
    // A clean result leaves no notice.
    await waitFor(() => expect(screen.queryByTestId(/^notice-/)).toBeNull());
  });

  it('409 destination_exists asks to overwrite; confirm re-calls with overwrite', async () => {
    mocks.copyFileToProject
      .mockRejectedValueOnce(await destinationExists())
      .mockResolvedValueOnce({ ...OK, moved: true });
    render(<RightPanel conversationId="c1" projectId="p1" />);
    fireEvent.click(screen.getByRole('button', { name: 'Move to project a.csv' }));

    const dialog = await screen.findByRole('dialog');
    expect(dialog.textContent).toContain('"a.csv" already exists in Project Files');
    fireEvent.click(within(dialog).getByRole('button', { name: 'Move and replace' }));

    await waitFor(() => expect(mocks.copyFileToProject).toHaveBeenCalledTimes(2));
    expect(mocks.copyFileToProject).toHaveBeenLastCalledWith('c1', { path: '/a.csv', move: true, includeHidden: false, overwrite: true });
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(screen.queryByTestId(/^notice-/)).toBeNull();
  });

  it('cancelling the overwrite prompt does nothing more', async () => {
    mocks.copyFileFromProject.mockRejectedValueOnce(await destinationExists());
    render(<RightPanel conversationId="c1" projectId="p1" />);
    fireEvent.click(screen.getByRole('button', { name: 'Copy to chat data' }));

    const dialog = await screen.findByRole('dialog');
    expect(dialog.textContent).toContain('A folder with this name already exists in Chat Files');
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }));

    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(mocks.copyFileFromProject).toHaveBeenCalledTimes(1);
    expect(screen.queryByTestId(/^notice-/)).toBeNull();
  });

  it('a failed overwrite retry stays in the prompt with the server message', async () => {
    const { FileApiError } = await import('../api/fileApi');
    mocks.copyFileToProject
      .mockRejectedValueOnce(await destinationExists())
      .mockRejectedValueOnce(new FileApiError('Cannot replace a folder with a file', 400, 'invalid_destination'));
    render(<RightPanel conversationId="c1" projectId="p1" />);
    fireEvent.click(screen.getByRole('button', { name: 'Copy to project a.csv' }));
    const dialog = await screen.findByRole('dialog');
    fireEvent.click(within(dialog).getByRole('button', { name: 'Copy and replace' }));
    expect(await within(dialog).findByRole('alert')).toHaveProperty(
      'textContent', 'Cannot replace a folder with a file',
    );
  });

  it('a move that could not remove the source shows a warning on the source card', async () => {
    mocks.copyFileToProject.mockResolvedValue({ ...OK, moved: false });
    render(<RightPanel conversationId="c1" projectId="p1" />);
    fireEvent.click(screen.getByRole('button', { name: 'Move to project a.csv' }));
    const notice = await screen.findByTestId('notice-Chat Files');
    expect(notice.dataset.tone).toBe('warning');
    expect(notice.textContent).toContain(
      'Copied "a.csv" to Project Files, but it could not be removed from Chat Files.',
    );
    fireEvent.click(within(notice).getByRole('button', { name: 'Dismiss' }));
    expect(screen.queryByTestId('notice-Chat Files')).toBeNull();
  });

  it('other errors surface the server message on the source card', async () => {
    const { FileApiError } = await import('../api/fileApi');
    mocks.copyFileToProject.mockRejectedValue(
      new FileApiError('pasted/ holds conversation scratch files', 400, 'forbidden_source'),
    );
    render(<RightPanel conversationId="c1" projectId="p1" />);
    fireEvent.click(screen.getByRole('button', { name: 'Copy to project a.csv' }));
    const notice = await screen.findByTestId('notice-Chat Files');
    expect(notice.dataset.tone).toBe('error');
    expect(notice.textContent).toContain(
      'Could not copy "a.csv" to Project Files: pasted/ holds conversation scratch files',
    );
    expect(screen.queryByRole('dialog')).toBeNull();
  });

  it('disables the row actions while that copy is in flight', async () => {
    let resolve!: (v: unknown) => void;
    mocks.copyFileToProject.mockReturnValue(new Promise((r) => { resolve = r; }));
    render(<RightPanel conversationId="c1" projectId="p1" />);
    fireEvent.click(screen.getByRole('button', { name: 'Copy to project a.csv' }));
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Move to project a.csv' })).toHaveProperty('disabled', true));
    expect(screen.getByRole('button', { name: 'Copy to project data' })).toHaveProperty('disabled', false);
    await act(async () => resolve(OK));
    expect(screen.getByRole('button', { name: 'Move to project a.csv' })).toHaveProperty('disabled', false);
  });
});

describe('Copy / Move edge cases', () => {
  it('scratch roots get no to-project actions; to-chat actions stay', () => {
    render(<RightPanel conversationId="c1" projectId="p1" />);
    const chat = within(screen.getByTestId('card-Chat Files'));
    const project = within(screen.getByTestId('card-Project Files'));
    expect(chat.queryByRole('button', { name: /pasted$/ })).toBeNull();
    expect(chat.queryByRole('button', { name: /\.responses$/ })).toBeNull();
    expect(chat.getByRole('button', { name: 'Copy to project data' })).toBeTruthy();
    expect(project.getByRole('button', { name: 'Copy to chat pasted' })).toBeTruthy();
    expect(project.getByRole('button', { name: 'Move to chat .responses' })).toBeTruthy();
  });

  it('includeHidden follows the card\'s show-hidden toggle, also on the overwrite retry', async () => {
    mocks.showHidden = true;
    mocks.copyFileFromProject
      .mockRejectedValueOnce(await destinationExists())
      .mockResolvedValueOnce({ ...OK, type: 'folder', path: '/data' });
    render(<RightPanel conversationId="c1" projectId="p1" />);
    fireEvent.click(screen.getByRole('button', { name: 'Copy to chat data' }));
    await waitFor(() =>
      expect(mocks.copyFileFromProject).toHaveBeenCalledWith('c1', { path: '/data', move: false, includeHidden: true }));
    const dialog = await screen.findByRole('dialog');
    fireEvent.click(within(dialog).getByRole('button', { name: 'Copy and replace' }));
    await waitFor(() => expect(mocks.copyFileFromProject).toHaveBeenLastCalledWith(
      'c1', { path: '/data', move: false, includeHidden: true, overwrite: true },
    ));
  });

  it('skipped entries show a warning on the source card', async () => {
    mocks.copyFileToProject.mockResolvedValueOnce({ type: 'folder', path: '/data', files_copied: 3, skipped: 2, moved: true });
    render(<RightPanel conversationId="c1" projectId="p1" />);
    fireEvent.click(screen.getByRole('button', { name: 'Move to project data' }));
    const notice = await screen.findByTestId('notice-Chat Files');
    expect(notice.dataset.tone).toBe('warning');
    expect(notice.textContent).toContain(
      'Moved "data" to Project Files; 2 hidden or linked entries were left in Chat Files.',
    );
  });

  it('skipped entries on a copy say they were not copied', async () => {
    mocks.copyFileFromProject.mockResolvedValueOnce({ type: 'folder', path: '/data', files_copied: 3, skipped: 1, moved: false });
    render(<RightPanel conversationId="c1" projectId="p1" />);
    fireEvent.click(screen.getByRole('button', { name: 'Copy to chat data' }));
    const notice = await screen.findByTestId('notice-Project Files');
    expect(notice.textContent).toContain('Copied "data" to Chat Files; 1 hidden or linked entry was not copied.');
  });

  it('a copy settling after the conversation changed sets no notice or prompt', async () => {
    let rejectFirst!: (e: unknown) => void;
    let resolveSecond!: (v: unknown) => void;
    mocks.copyFileToProject
      .mockReturnValueOnce(new Promise((_r, rej) => { rejectFirst = rej; }))
      .mockReturnValueOnce(new Promise((r) => { resolveSecond = r; }));
    const { rerender } = render(<RightPanel conversationId="c1" projectId="p1" />);
    fireEvent.click(screen.getByRole('button', { name: 'Copy to project a.csv' }));
    fireEvent.click(screen.getByRole('button', { name: 'Move to project data' }));
    rerender(<RightPanel conversationId="c2" projectId="p1" />);
    const exists = await destinationExists();
    await act(async () => {
      rejectFirst(exists);
      resolveSecond({ ...OK, moved: false });
    });
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(screen.queryByTestId(/^notice-/)).toBeNull();
  });

  it('row actions are disabled while the overwrite prompt is open', async () => {
    mocks.copyFileToProject.mockRejectedValueOnce(await destinationExists());
    render(<RightPanel conversationId="c1" projectId="p1" />);
    fireEvent.click(screen.getByRole('button', { name: 'Copy to project a.csv' }));
    await screen.findByRole('dialog');
    expect(screen.getByRole('button', { name: 'Copy to project data' })).toHaveProperty('disabled', true);
    expect(screen.getByRole('button', { name: 'Copy to chat data' })).toHaveProperty('disabled', true);
  });
});

describe('vertical split', () => {
  const flexGrow = (key: string) => screen.getByTestId(`right-panel-card-${key}`).style.flexGrow;

  it('reads a stored three-card split back', () => {
    localStorage.setItem(getSplit3StorageKey('p1'), JSON.stringify([20, 50]));
    render(<RightPanel conversationId="c1" projectId="p1" />);
    expect(flexGrow('chat')).toBe('20');
    expect(flexGrow('project')).toBe('50');
    expect(flexGrow('tables')).toBe('30');
  });

  it('derives the three-card split from a legacy two-card value', () => {
    localStorage.setItem(getSplitStorageKey('p1'), '70');
    render(<RightPanel conversationId="c1" projectId="p1" />);
    expect(flexGrow('chat')).toBe('35');
    expect(flexGrow('project')).toBe('35');
    expect(flexGrow('tables')).toBe('30');
  });

  it('reads the two-card key on the drilled home screen', () => {
    mocks.drilledProjectId = 'p1';
    localStorage.setItem(getSplitStorageKey('p1'), '25');
    render(<RightPanel conversationId={null} projectId={null} />);
    expect(flexGrow('project')).toBe('25');
    expect(flexGrow('tables')).toBe('75');
  });

  it('dragging the second divider persists the three-card split', () => {
    render(<RightPanel conversationId="c1" projectId="p1" />);
    const panel = screen.getByTestId('right-panel-card-chat').parentElement!;
    vi.spyOn(panel, 'getBoundingClientRect').mockReturnValue(
      { top: 0, height: 1000, left: 0, right: 280, bottom: 1000, width: 280, x: 0, y: 0, toJSON: () => ({}) },
    );
    const divider = screen.getByTestId('right-panel-divider-1');
    fireEvent.pointerDown(divider, { clientY: 750, pointerId: 1, button: 0 });
    fireEvent.pointerMove(divider, { clientY: 900, pointerId: 1 });
    fireEvent.pointerUp(divider, { clientY: 900, pointerId: 1 });
    // Defaults [40, 35, 25]: the boundary below Project Files moves to 90%.
    expect(JSON.parse(localStorage.getItem(getSplit3StorageKey('p1'))!)).toEqual([40, 50]);
    expect(flexGrow('tables')).toBe('10');
  });

  it('moveSplitBoundary keeps both neighbours at the minimum', () => {
    expect(moveSplitBoundary([40, 35, 25], 0, 2, 8)).toEqual([8, 67, 25]);
    expect(moveSplitBoundary([40, 35, 25], 1, 99, 8)).toEqual([40, 52, 8]);
    // A pair too small for two minimums is split evenly.
    expect(moveSplitBoundary([10, 5, 85], 0, 50, 8)).toEqual([7.5, 7.5, 85]);
  });

  it('a click without movement saves nothing', () => {
    render(<RightPanel conversationId="c1" projectId="p1" />);
    const panel = screen.getByTestId('right-panel-card-chat').parentElement!;
    vi.spyOn(panel, 'getBoundingClientRect').mockReturnValue(
      { top: 0, height: 1000, left: 0, right: 280, bottom: 1000, width: 280, x: 0, y: 0, toJSON: () => ({}) },
    );
    const divider = screen.getByTestId('right-panel-divider-0');
    fireEvent.pointerDown(divider, { clientY: 400, pointerId: 1, button: 0 });
    fireEvent.pointerUp(divider, { clientY: 400, pointerId: 1 });
    expect(localStorage.getItem(getSplit3StorageKey('p1'))).toBeNull();
  });

  it.each([
    ['not JSON', '{oops'],
    ['wrong arity', '[30]'],
    ['strings', '["30", "30"]'],
    ['negative', '[-10, 50]'],
    ['sum over 100', '[80, 40]'],
  ])('a malformed or out-of-range three-card split (%s) falls back to the defaults', (_label, raw) => {
    localStorage.setItem(getSplit3StorageKey('p1'), raw);
    render(<RightPanel conversationId="c1" projectId="p1" />);
    expect(flexGrow('chat')).toBe('40');
    expect(flexGrow('project')).toBe('35');
    expect(flexGrow('tables')).toBe('25');
  });

  it('a stored near-zero share is lifted to the minimum', () => {
    localStorage.setItem(getSplit3StorageKey('p1'), JSON.stringify([0.5, 49.5]));
    render(<RightPanel conversationId="c1" projectId="p1" />);
    expect(Number(flexGrow('chat'))).toBeCloseTo(5);
    const total = ['chat', 'project', 'tables'].reduce((acc, k) => acc + Number(flexGrow(k)), 0);
    expect(total).toBeCloseTo(100);
  });

  it('switching from drilled home (2 cards) to a project conversation (3 cards) loads the right key', () => {
    mocks.drilledProjectId = 'p1';
    localStorage.setItem(getSplitStorageKey('p1'), '25');
    localStorage.setItem(getSplit3StorageKey('p1'), JSON.stringify([20, 50]));
    const { rerender } = render(<RightPanel conversationId={null} projectId={null} />);
    expect(flexGrow('project')).toBe('25');
    rerender(<RightPanel conversationId="c1" projectId="p1" />);
    expect(flexGrow('chat')).toBe('20');
    expect(flexGrow('project')).toBe('50');
    expect(flexGrow('tables')).toBe('30');
  });

  it('a project change reloads the shares', () => {
    localStorage.setItem(getSplit3StorageKey('p1'), JSON.stringify([20, 50]));
    localStorage.setItem(getSplit3StorageKey('p2'), JSON.stringify([60, 20]));
    const { rerender } = render(<RightPanel conversationId="c1" projectId="p1" />);
    expect(flexGrow('chat')).toBe('20');
    rerender(<RightPanel conversationId="c2" projectId="p2" />);
    expect(flexGrow('chat')).toBe('60');
    expect(flexGrow('tables')).toBe('20');
  });

  it('normalizeShares rejects garbage and impossible floors', () => {
    expect(normalizeShares([NaN, 50, 50])).toBeNull();
    expect(normalizeShares([0, 50, 50])).toBeNull();
    expect(normalizeShares([50, 50], 60)).toBeNull();
    expect(normalizeShares([20, 50, 30])).toEqual([20, 50, 30]);
  });
});
