// The hidden-data download acknowledgement: silent for plain text, a blocking
// dialog for everything else, resolved by Acknowledge / Cancel / Escape, and
// the useWorkspaceDownload hook that only fetches after an acknowledgement.
import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { ModalShell } from '../components/ModalShell';
import { useWorkspaceDownload } from '../hooks/useWorkspaceDownload';
import { DownloadWarningProvider, useDownloadWarning } from './DownloadWarningContext';

const mocks = vi.hoisted(() => ({
  downloadFile: vi.fn(),
  saveBlobToDisk: vi.fn(),
}));

vi.mock('../api/fileApi', () => ({
  downloadFile: mocks.downloadFile,
  saveBlobToDisk: mocks.saveBlobToDisk,
}));

/** Exposes confirmDownload to the test and records every verdict. */
function ConfirmProbe({ verdicts, name, kind = 'file' }: {
  verdicts: boolean[];
  name: string;
  kind?: 'file' | 'folder';
}) {
  const { confirmDownload } = useDownloadWarning();
  return (
    <button type="button" onClick={() => void confirmDownload({ name, kind }).then((v) => verdicts.push(v))}>
      Ask
    </button>
  );
}

function renderProbe(name: string, kind: 'file' | 'folder' = 'file') {
  const verdicts: boolean[] = [];
  render(
    <DownloadWarningProvider>
      <ConfirmProbe verdicts={verdicts} name={name} kind={kind} />
    </DownloadWarningProvider>,
  );
  return verdicts;
}

const dialog = () => screen.queryByRole('dialog', { name: 'This download may carry hidden data' });

describe('DownloadWarningProvider', () => {
  afterEach(() => {
    cleanup();
    mocks.downloadFile.mockReset();
    mocks.saveBlobToDisk.mockReset();
  });

  it('resolves plain-text downloads at once without a dialog', async () => {
    const verdicts = renderProbe('notes.txt');
    fireEvent.click(screen.getByRole('button', { name: 'Ask' }));
    await waitFor(() => expect(verdicts).toEqual([true]));
    expect(dialog()).toBeNull();
  });

  it('opens the warning for a sensitive type and resolves true on Acknowledge and Download', async () => {
    const verdicts = renderProbe('report.html');
    fireEvent.click(screen.getByRole('button', { name: 'Ask' }));

    expect(dialog()).toBeTruthy();
    expect(screen.getByText('report.html')).toBeTruthy();
    expect(screen.getByText(/scripts that run as soon as the file is opened/)).toBeTruthy();
    expect(verdicts).toEqual([]);

    fireEvent.click(screen.getByRole('button', { name: 'Acknowledge and Download' }));
    await waitFor(() => expect(verdicts).toEqual([true]));
    expect(dialog()).toBeNull();
  });

  it('resolves false on Cancel', async () => {
    const verdicts = renderProbe('photo.png');
    fireEvent.click(screen.getByRole('button', { name: 'Ask' }));
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    await waitFor(() => expect(verdicts).toEqual([false]));
    expect(dialog()).toBeNull();
  });

  it('uses the severe form for code: danger title, do-not-run paragraph, danger tone', () => {
    renderProbe('setup.py');
    fireEvent.click(screen.getByRole('button', { name: 'Ask' }));
    expect(screen.getByRole('dialog', { name: 'This download may be harmful to run' })).toBeTruthy();
    expect(screen.getByText(/Source code can do anything when it runs/)).toBeTruthy();
    expect(screen.getByRole('alert').textContent).toMatch(/Do not run, execute, import, compile or install/);
    const confirm = screen.getByRole('button', { name: 'Acknowledge and Download' });
    expect(confirm.className).toContain('doc-dialog-confirm--danger');
  });

  it('warns for markup about rendered remote references, in the plain form', () => {
    renderProbe('README.md');
    fireEvent.click(screen.getByRole('button', { name: 'Ask' }));
    expect(dialog()).toBeTruthy();
    expect(screen.getByText(/a markdown image URL, for example/)).toBeTruthy();
    expect(screen.queryByRole('alert')).toBeNull();
    const confirm = screen.getByRole('button', { name: 'Acknowledge and Download' });
    expect(confirm.className).toContain('doc-dialog-confirm--default');
  });

  it('names a folder download as a zip archive', () => {
    renderProbe('reports', 'folder');
    fireEvent.click(screen.getByRole('button', { name: 'Ask' }));
    expect(screen.getByText('reports')).toBeTruthy();
    expect(screen.getByText(/will be downloaded as a zip archive/)).toBeTruthy();
    expect(screen.getByText(/Every file inside carries the same risks/)).toBeTruthy();
  });

  it('shows the warning on every download, with no memory of earlier acknowledgements', async () => {
    const verdicts = renderProbe('photo.png');
    fireEvent.click(screen.getByRole('button', { name: 'Ask' }));
    fireEvent.click(screen.getByRole('button', { name: 'Acknowledge and Download' }));
    await waitFor(() => expect(verdicts).toEqual([true]));

    fireEvent.click(screen.getByRole('button', { name: 'Ask' }));
    expect(dialog()).toBeTruthy();
  });

  it('Escape cancels the warning alone, leaving a modal underneath it open', async () => {
    // A ModalShell host (the file viewer) with its own document-level Escape.
    const hostClose = vi.fn();
    const verdicts: boolean[] = [];
    render(
      <DownloadWarningProvider>
        <ModalShell isOpen onClose={hostClose} overlayClassName="host">
          <ConfirmProbe verdicts={verdicts} name="deck.pptx" />
        </ModalShell>
      </DownloadWarningProvider>,
    );
    fireEvent.click(screen.getByRole('button', { name: 'Ask' }));
    expect(dialog()).toBeTruthy();

    fireEvent.keyDown(document.body, { key: 'Escape' });
    await waitFor(() => expect(verdicts).toEqual([false]));
    expect(dialog()).toBeNull();
    expect(hostClose).not.toHaveBeenCalled();

    // With the warning gone, Escape reaches the host again.
    fireEvent.keyDown(document.body, { key: 'Escape' });
    expect(hostClose).toHaveBeenCalledTimes(1);
  });

  it('a second request while one is open cancels the first and takes the dialog over', async () => {
    const verdicts: boolean[] = [];
    render(
      <DownloadWarningProvider>
        <ConfirmProbe verdicts={verdicts} name="first.pdf" />
        <ConfirmProbe verdicts={verdicts} name="second.zip" />
      </DownloadWarningProvider>,
    );
    const [askFirst, askSecond] = screen.getAllByRole('button', { name: 'Ask' });
    fireEvent.click(askFirst);
    fireEvent.click(askSecond);
    await waitFor(() => expect(verdicts).toEqual([false]));
    expect(screen.queryByText('first.pdf')).toBeNull();
    expect(screen.getByText('second.zip')).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: 'Acknowledge and Download' }));
    await waitFor(() => expect(verdicts).toEqual([false, true]));
  });
});

describe('useWorkspaceDownload', () => {
  afterEach(() => {
    cleanup();
    mocks.downloadFile.mockReset();
    mocks.saveBlobToDisk.mockReset();
  });

  function DownloadProbe({ path, results }: { path: string; results: boolean[] }) {
    const download = useWorkspaceDownload();
    return (
      <button type="button" onClick={() => void download('conv-1', path).then((r) => results.push(r))}>
        Download
      </button>
    );
  }

  it('fetches and saves a plain-text file without asking', async () => {
    mocks.downloadFile.mockResolvedValue({ url: 'blob:1', filename: 'notes.txt' });
    const results: boolean[] = [];
    render(
      <DownloadWarningProvider>
        <DownloadProbe path="out/notes.txt" results={results} />
      </DownloadWarningProvider>,
    );
    fireEvent.click(screen.getByRole('button', { name: 'Download' }));
    await waitFor(() => expect(results).toEqual([true]));
    expect(mocks.downloadFile).toHaveBeenCalledWith('conv-1', 'out/notes.txt');
    expect(mocks.saveBlobToDisk).toHaveBeenCalledWith({ url: 'blob:1', filename: 'notes.txt' });
  });

  it('does not fetch a sensitive file until the warning is acknowledged, and never after Cancel', async () => {
    mocks.downloadFile.mockResolvedValue({ url: 'blob:2', filename: 'chart.png' });
    const results: boolean[] = [];
    render(
      <DownloadWarningProvider>
        <DownloadProbe path="charts/chart.png" results={results} />
      </DownloadWarningProvider>,
    );
    const button = screen.getByRole('button', { name: 'Download' });

    fireEvent.click(button);
    expect(dialog()).toBeTruthy();
    expect(mocks.downloadFile).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    await waitFor(() => expect(results).toEqual([false]));
    expect(mocks.downloadFile).not.toHaveBeenCalled();

    fireEvent.click(button);
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Acknowledge and Download' }));
    });
    await waitFor(() => expect(results).toEqual([false, true]));
    expect(mocks.downloadFile).toHaveBeenCalledWith('conv-1', 'charts/chart.png');
    expect(mocks.saveBlobToDisk).toHaveBeenCalledWith({ url: 'blob:2', filename: 'chart.png' });
  });
});
