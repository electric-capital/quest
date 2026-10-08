// FileBrowser per file space: the card title, which routes it calls
// (conversation vs project), the per-source file_list_changed filter, the
// per-source navigation/dotfile state, and the rowActions extension point.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import type { FileEntry } from '../api/types';
import type { FileSource } from '../api/fileApi';
import { FileBrowserStateProvider } from '../contexts/FileBrowserStateContext';
import { DownloadWarningProvider } from '../contexts/DownloadWarningContext';
import { FileBrowser, fileListEventMatchesSource } from './FileBrowser';

const mocks = vi.hoisted(() => ({
  listFiles: vi.fn(),
  uploadFiles: vi.fn(),
  deleteFile: vi.fn(),
  getFileInfo: vi.fn(),
  downloadFile: vi.fn(),
  downloadFolder: vi.fn(),
  saveBlobToDisk: vi.fn(),
  globalHandlers: new Set<(event: Record<string, unknown>) => void>(),
}));

vi.mock('../api/fileApi', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/fileApi')>();
  return {
    ...actual,
    listFiles: mocks.listFiles,
    uploadFiles: mocks.uploadFiles,
    deleteFile: mocks.deleteFile,
    getFileInfo: mocks.getFileInfo,
    downloadFile: mocks.downloadFile,
    downloadFolder: mocks.downloadFolder,
    saveBlobToDisk: mocks.saveBlobToDisk,
  };
});

vi.mock('../services/PersistentWebSocket', () => ({
  persistentWebSocket: {
    onGlobalEvent: (handler: (event: Record<string, unknown>) => void) => {
      mocks.globalHandlers.add(handler);
      return () => mocks.globalHandlers.delete(handler);
    },
  },
}));

// The real modal pulls in the PDF viewer; a stub records its source.
vi.mock('./FileViewerModal', () => ({
  FileViewerModal: (props: { source?: FileSource; filePath: string }) => (
    <div data-testid="file-viewer">{props.source?.kind}:{props.source?.id}|{props.filePath}</div>
  ),
}));

const CHAT: FileSource = { kind: 'conversation', id: 'c1' };
const PROJECT: FileSource = { kind: 'project', id: 'p1' };

const entry = (name: string, type: 'file' | 'folder' = 'file'): FileEntry => ({
  name, type, size: type === 'file' ? 10 : null, lastModified: '2026-10-01T00:00:00Z',
});

/** listFiles answers per (space, path). */
function serveListing(listings: Record<string, FileEntry[]>) {
  mocks.listFiles.mockImplementation(async (source: FileSource, path: string) => {
    const currentPath = path || '/';
    return {
      currentPath,
      files: listings[`${source.kind}:${source.id}:${currentPath}`] ?? [],
      canGoUp: currentPath !== '/',
    };
  });
}

function emit(event: Record<string, unknown>) {
  act(() => {
    for (const handler of [...mocks.globalHandlers]) handler(event);
  });
}

function renderCards(ui: React.ReactElement) {
  return render(
    <DownloadWarningProvider>
      <FileBrowserStateProvider>{ui}</FileBrowserStateProvider>
    </DownloadWarningProvider>,
  );
}

/** The card element whose heading is `title`. */
function card(title: string): HTMLElement {
  return screen.getByRole('heading', { name: title }).closest('.file-browser') as HTMLElement;
}

beforeEach(() => {
  serveListing({});
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  mocks.listFiles.mockReset();
  mocks.uploadFiles.mockReset();
  mocks.deleteFile.mockReset();
  mocks.getFileInfo.mockReset();
  mocks.downloadFile.mockReset();
  mocks.downloadFolder.mockReset();
  mocks.saveBlobToDisk.mockReset();
  mocks.globalHandlers.clear();
});

describe('FileBrowser source', () => {
  it('titles a conversation card "Chat Files" and lists via the conversation source', async () => {
    renderCards(<FileBrowser source={CHAT} />);
    expect(screen.getByRole('heading', { name: 'Chat Files' })).toBeTruthy();
    await waitFor(() => expect(mocks.listFiles).toHaveBeenCalledWith(CHAT, '/'));
  });

  it('titles a project card "Project Files" and lists via the project source', async () => {
    renderCards(<FileBrowser source={PROJECT} />);
    expect(screen.getByRole('heading', { name: 'Project Files' })).toBeTruthy();
    await waitFor(() => expect(mocks.listFiles).toHaveBeenCalledWith(PROJECT, '/'));
    expect(mocks.listFiles.mock.calls.every(([s]) => (s as FileSource).kind === 'project')).toBe(true);
    expect(await screen.findByText(/Shared by every chat in this project; chats reach these files as proj:\/\/ paths/)).toBeTruthy();
  });

  it('renders the no-conversation empty state for a null source without fetching', () => {
    renderCards(<FileBrowser source={null} />);
    expect(screen.getByText('Select a conversation to browse files')).toBeTruthy();
    expect(mocks.listFiles).not.toHaveBeenCalled();
  });

  it('uploads and deletes through the card\'s own source', async () => {
    serveListing({ 'project:p1:/': [entry('a.txt')] });
    mocks.uploadFiles.mockResolvedValue({ uploadedFiles: [], errors: [] });
    mocks.deleteFile.mockResolvedValue({ name: 'a.txt', type: 'file', deletedCount: 1 });
    vi.spyOn(window, 'confirm').mockReturnValue(true);

    const { container } = renderCards(<FileBrowser source={PROJECT} />);
    await screen.findByText('a.txt');

    const input = container.querySelector('input[type="file"]') as HTMLInputElement;
    const file = new File(['x'], 'up.txt');
    fireEvent.change(input, { target: { files: [file] } });
    await waitFor(() => expect(mocks.uploadFiles).toHaveBeenCalled());
    expect(mocks.uploadFiles.mock.calls[0][0]).toEqual(PROJECT);

    fireEvent.click(screen.getByTitle('More options'));
    fireEvent.click(screen.getByRole('button', { name: 'Delete' }));
    await waitFor(() => expect(mocks.deleteFile).toHaveBeenCalledWith(PROJECT, '/a.txt'));
  });

  it('opens the viewer on the card\'s source', async () => {
    serveListing({ 'project:p1:/': [entry('notes.md')] });
    renderCards(<FileBrowser source={PROJECT} />);
    fireEvent.click(await screen.findByText('notes.md'));
    expect(screen.getByTestId('file-viewer').textContent).toBe('project:p1|/notes.md');
  });
});

describe('file_list_changed filter', () => {
  it('maps conversation-scope events to the Chat Files card and project-scope events to Project Files', () => {
    const conv = { type: 'file_list_changed', scope: 'conversation', conversation_id: 'c1', project_id: 'p1' };
    const proj = { type: 'file_list_changed', scope: 'project', conversation_id: null, project_id: 'p1' };
    expect(fileListEventMatchesSource(conv, CHAT)).toBe(true);
    expect(fileListEventMatchesSource(conv, PROJECT)).toBe(false);
    expect(fileListEventMatchesSource(proj, PROJECT)).toBe(true);
    expect(fileListEventMatchesSource(proj, CHAT)).toBe(false);
    expect(fileListEventMatchesSource({ ...conv, conversation_id: 'c2' }, CHAT)).toBe(false);
    expect(fileListEventMatchesSource({ ...proj, project_id: 'p2' }, PROJECT)).toBe(false);
  });

  it('refreshes only the card whose space changed', async () => {
    renderCards(
      <>
        <FileBrowser source={CHAT} />
        <FileBrowser source={PROJECT} />
      </>,
    );
    await waitFor(() => expect(mocks.listFiles).toHaveBeenCalledTimes(2));
    const callsFor = (kind: string) =>
      mocks.listFiles.mock.calls.filter(([s]) => (s as FileSource).kind === kind).length;

    vi.useFakeTimers();
    // Mismatched ids and unrelated events: no refetch.
    emit({ type: 'file_list_changed', scope: 'conversation', conversation_id: 'other', project_id: 'p1' });
    emit({ type: 'file_list_changed', scope: 'project', conversation_id: null, project_id: 'other' });
    emit({ type: 'conversation_list_changed', scope: 'project', project_id: 'p1' });
    await act(async () => { await vi.advanceTimersByTimeAsync(500); });
    expect(callsFor('conversation')).toBe(1);
    expect(callsFor('project')).toBe(1);

    emit({ type: 'file_list_changed', scope: 'conversation', conversation_id: 'c1', project_id: 'p1' });
    await act(async () => { await vi.advanceTimersByTimeAsync(500); });
    expect(callsFor('conversation')).toBe(2);
    expect(callsFor('project')).toBe(1);

    emit({ type: 'file_list_changed', scope: 'project', conversation_id: null, project_id: 'p1' });
    await act(async () => { await vi.advanceTimersByTimeAsync(500); });
    expect(callsFor('conversation')).toBe(2);
    expect(callsFor('project')).toBe(2);
  });
});

describe('per-source state', () => {
  it('keeps independent paths and dotfile toggles for two sources', async () => {
    serveListing({
      'conversation:c1:/': [entry('reports', 'folder'), entry('.hidden-chat')],
      'conversation:c1:/reports': [entry('q3.csv')],
      'project:p1:/': [entry('shared.md'), entry('.hidden-proj')],
    });
    renderCards(
      <>
        <FileBrowser source={CHAT} />
        <FileBrowser source={PROJECT} />
      </>,
    );
    const chat = card('Chat Files');
    const project = card('Project Files');

    fireEvent.click(await within(chat).findByText('reports'));
    await within(chat).findByText('q3.csv');
    expect(within(chat).getByTitle('/reports')).toBeTruthy();
    expect(within(project).getByTitle('/')).toBeTruthy();
    expect(within(project).getByText('shared.md')).toBeTruthy();

    expect(within(project).queryByText('.hidden-proj')).toBeNull();
    fireEvent.click(within(project).getByTitle('Show hidden files'));
    expect(within(project).getByText('.hidden-proj')).toBeTruthy();
    expect(within(chat).getByTitle('Show hidden files')).toBeTruthy();
  });
});

describe('rowActions extension point', () => {
  it('renders host actions between Download and Delete with the row context', async () => {
    serveListing({ 'conversation:c1:/': [entry('a.csv')] });
    const onSelect = vi.fn();
    const rowActions = vi.fn(() => [{ key: 'copy', label: 'Copy to project', onSelect }]);
    renderCards(<FileBrowser source={CHAT} rowActions={rowActions} />);

    fireEvent.click(await screen.findByTitle('More options'));
    const labels = screen.getAllByRole('button').map((b) => b.textContent);
    expect(labels.indexOf('Copy to project')).toBeGreaterThan(labels.indexOf('Download'));
    expect(labels.indexOf('Copy to project')).toBeLessThan(labels.indexOf('Delete'));
    expect(rowActions).toHaveBeenCalledWith({ entry: entry('a.csv'), path: '/a.csv', source: CHAT });

    fireEvent.click(screen.getByRole('button', { name: 'Copy to project' }));
    expect(onSelect).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole('button', { name: 'Copy to project' })).toBeNull();
  });
});

describe('source switch race', () => {
  it('never shows a stale listing that resolves after the switch', async () => {
    let resolveC1: (value: unknown) => void = () => {};
    mocks.listFiles.mockImplementation((source: FileSource) => {
      if (source.id === 'c1') {
        return new Promise((resolve) => { resolveC1 = resolve; });
      }
      return Promise.resolve({ currentPath: '/', files: [entry('c2-file.txt')], canGoUp: false });
    });

    const ui = (source: FileSource) => (
      <DownloadWarningProvider>
        <FileBrowserStateProvider><FileBrowser source={source} /></FileBrowserStateProvider>
      </DownloadWarningProvider>
    );
    const { rerender } = render(ui(CHAT));
    await waitFor(() => expect(mocks.listFiles).toHaveBeenCalledWith(CHAT, '/'));

    const c2: FileSource = { kind: 'conversation', id: 'c2' };
    rerender(ui(c2));
    await screen.findByText('c2-file.txt');

    await act(async () => {
      resolveC1({ currentPath: '/', files: [entry('c1-stale.txt')], canGoUp: false });
    });
    expect(screen.queryByText('c1-stale.txt')).toBeNull();
    expect(screen.getByText('c2-file.txt')).toBeTruthy();
  });
});

describe('project-card downloads', () => {
  const warning = () => screen.queryByRole('dialog', { name: 'This download may carry hidden data' });

  it('shows the hidden-data warning before downloading a file from the project source', async () => {
    serveListing({ 'project:p1:/': [entry('report.html')] });
    mocks.downloadFile.mockResolvedValue({ url: 'blob:x', filename: 'report.html' });
    renderCards(<FileBrowser source={PROJECT} />);

    fireEvent.click(await screen.findByTitle('More options'));
    fireEvent.click(screen.getByRole('button', { name: 'Download' }));
    await waitFor(() => expect(warning()).toBeTruthy());
    expect(mocks.downloadFile).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: 'Acknowledge and Download' }));
    await waitFor(() => expect(mocks.downloadFile).toHaveBeenCalledWith(PROJECT, '/report.html'));
    expect(mocks.saveBlobToDisk).toHaveBeenCalledWith({ url: 'blob:x', filename: 'report.html' });
  });

  it('shows the warning before zipping a project folder, and Cancel downloads nothing', async () => {
    serveListing({ 'project:p1:/': [entry('out', 'folder')] });
    mocks.downloadFolder.mockResolvedValue({ url: 'blob:z', filename: 'out.zip' });
    renderCards(<FileBrowser source={PROJECT} />);

    fireEvent.click(await screen.findByTitle('More options'));
    fireEvent.click(screen.getByRole('button', { name: 'Download as Zip' }));
    await waitFor(() => expect(warning()).toBeTruthy());
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    await waitFor(() => expect(warning()).toBeNull());
    expect(mocks.downloadFolder).not.toHaveBeenCalled();

    fireEvent.click(screen.getByTitle('More options'));
    fireEvent.click(screen.getByRole('button', { name: 'Download as Zip' }));
    await waitFor(() => expect(warning()).toBeTruthy());
    expect(mocks.downloadFolder).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Acknowledge and Download' }));
    await waitFor(() => expect(mocks.downloadFolder).toHaveBeenCalledWith(PROJECT, '/out'));
  });
});
