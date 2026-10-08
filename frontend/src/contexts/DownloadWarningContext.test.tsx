// The hidden-data download acknowledgement: silent for plain text, a blocking
// dialog for everything else, resolved by Acknowledge / Cancel / Escape, and
// the useWorkspaceDownload hook that only fetches after an acknowledgement.
import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { ModalShell } from '../components/ModalShell';
import { useWorkspaceDownload } from '../hooks/useWorkspaceDownload';
import { DownloadWarningProvider, useDownloadWarning, type DownloadDecision } from './DownloadWarningContext';

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
  verdicts: DownloadDecision[];
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
  const verdicts: DownloadDecision[] = [];
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
    await waitFor(() => expect(verdicts).toEqual(['original']));
    expect(dialog()).toBeNull();
  });

  it("opens the warning for a sensitive type and resolves 'original' on Acknowledge and Download", async () => {
    const verdicts = renderProbe('report.html');
    fireEvent.click(screen.getByRole('button', { name: 'Ask' }));

    expect(dialog()).toBeTruthy();
    expect(screen.getByText('report.html')).toBeTruthy();
    expect(screen.getByText(/scripts that run as soon as the file is opened/)).toBeTruthy();
    expect(verdicts).toEqual([]);

    fireEvent.click(screen.getByRole('button', { name: 'Acknowledge and Download' }));
    await waitFor(() => expect(verdicts).toEqual(['original']));
    expect(dialog()).toBeNull();
  });

  it("resolves 'cancel' on Cancel", async () => {
    const verdicts = renderProbe('summary.pdf');
    fireEvent.click(screen.getByRole('button', { name: 'Ask' }));
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    await waitFor(() => expect(verdicts).toEqual(['cancel']));
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
    expect(screen.getByText(/a markdown image URL, or a formula cell/)).toBeTruthy();
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
    const verdicts = renderProbe('summary.pdf');
    fireEvent.click(screen.getByRole('button', { name: 'Ask' }));
    fireEvent.click(screen.getByRole('button', { name: 'Acknowledge and Download' }));
    await waitFor(() => expect(verdicts).toEqual(['original']));

    fireEvent.click(screen.getByRole('button', { name: 'Ask' }));
    expect(dialog()).toBeTruthy();
  });

  it('Escape cancels the warning alone, leaving a modal underneath it open', async () => {
    // A ModalShell host (the file viewer) with its own document-level Escape.
    const hostClose = vi.fn();
    const verdicts: DownloadDecision[] = [];
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
    await waitFor(() => expect(verdicts).toEqual(['cancel']));
    expect(dialog()).toBeNull();
    expect(hostClose).not.toHaveBeenCalled();

    // With the warning gone, Escape reaches the host again.
    fireEvent.keyDown(document.body, { key: 'Escape' });
    expect(hostClose).toHaveBeenCalledTimes(1);
  });

  it('a second request while one is open cancels the first and takes the dialog over', async () => {
    const verdicts: DownloadDecision[] = [];
    render(
      <DownloadWarningProvider>
        <ConfirmProbe verdicts={verdicts} name="first.pdf" />
        <ConfirmProbe verdicts={verdicts} name="second.zip" />
      </DownloadWarningProvider>,
    );
    const [askFirst, askSecond] = screen.getAllByRole('button', { name: 'Ask' });
    fireEvent.click(askFirst);
    fireEvent.click(askSecond);
    await waitFor(() => expect(verdicts).toEqual(['cancel']));
    expect(screen.queryByText('first.pdf')).toBeNull();
    expect(screen.getByText('second.zip')).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: 'Acknowledge and Download' }));
    await waitFor(() => expect(verdicts).toEqual(['cancel', 'original']));
  });
});

describe('DownloadWarningProvider -- sanitizable images', () => {
  afterEach(cleanup);

  const imageDialog = () => screen.queryByRole('dialog', { name: 'This image may carry hidden data' });
  const originalDialog = () => screen.queryByRole('dialog', { name: 'Download the original file?' });

  it('leads with the sanitized copy and resolves sanitized on the primary button', async () => {
    const verdicts = renderProbe('chart.png');
    fireEvent.click(screen.getByRole('button', { name: 'Ask' }));

    expect(imageDialog()).toBeTruthy();
    expect(screen.getByText(/keeps the picture exactly as it is/)).toBeTruthy();
    expect(screen.getByText(/"Generated with Quest" tag/)).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Acknowledge and Download' })).toBeNull();
    const primary = screen.getByRole('button', { name: 'Download Sanitized Copy' });
    expect(primary.className).toContain('doc-dialog-confirm--default');

    fireEvent.click(primary);
    await waitFor(() => expect(verdicts).toEqual(['sanitized']));
    expect(imageDialog()).toBeNull();
  });

  it('hands out the original only after the checkbox on the second dialog is ticked', async () => {
    const verdicts = renderProbe('photo.jpeg');
    fireEvent.click(screen.getByRole('button', { name: 'Ask' }));
    fireEvent.click(screen.getByRole('button', { name: 'Download Original…' }));

    expect(imageDialog()).toBeNull();
    expect(originalDialog()).toBeTruthy();
    expect(screen.getByText('photo.jpeg')).toBeTruthy();
    const confirm = screen.getByRole('button', { name: 'Download Original' });
    expect(confirm.className).toContain('doc-dialog-confirm--danger');
    expect((confirm as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(confirm);
    expect(verdicts).toEqual([]);

    fireEvent.click(screen.getByRole('checkbox', { name: /I know what I am doing/ }));
    expect((confirm as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(confirm);
    await waitFor(() => expect(verdicts).toEqual(['original']));
    expect(originalDialog()).toBeNull();
  });

  it('Cancel and Escape on the original confirm return to the warning with the checkbox cleared', async () => {
    const verdicts = renderProbe('anim.gif');
    fireEvent.click(screen.getByRole('button', { name: 'Ask' }));
    fireEvent.click(screen.getByRole('button', { name: 'Download Original…' }));
    fireEvent.click(screen.getByRole('checkbox'));
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));

    expect(originalDialog()).toBeNull();
    expect(imageDialog()).toBeTruthy();
    expect(verdicts).toEqual([]);

    fireEvent.click(screen.getByRole('button', { name: 'Download Original…' }));
    expect((screen.getByRole('checkbox') as HTMLInputElement).checked).toBe(false);
    fireEvent.keyDown(document.body, { key: 'Escape' });
    expect(originalDialog()).toBeNull();
    expect(imageDialog()).toBeTruthy();

    fireEvent.keyDown(document.body, { key: 'Escape' });
    await waitFor(() => expect(verdicts).toEqual(['cancel']));
    expect(imageDialog()).toBeNull();
  });

  it('keeps the plain warning for images the server cannot rewrite', () => {
    renderProbe('scan.tiff');
    fireEvent.click(screen.getByRole('button', { name: 'Ask' }));
    expect(screen.getByRole('dialog', { name: 'This download may carry hidden data' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Acknowledge and Download' })).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Download Original…' })).toBeNull();
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
    expect(mocks.downloadFile).toHaveBeenCalledWith('conv-1', 'out/notes.txt', 'original');
    expect(mocks.saveBlobToDisk).toHaveBeenCalledWith({ url: 'blob:1', filename: 'notes.txt' });
  });

  it('does not fetch a sensitive file until the warning is acknowledged, and never after Cancel', async () => {
    mocks.downloadFile.mockResolvedValue({ url: 'blob:2', filename: 'deck.pptx' });
    const results: boolean[] = [];
    render(
      <DownloadWarningProvider>
        <DownloadProbe path="slides/deck.pptx" results={results} />
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
    expect(mocks.downloadFile).toHaveBeenCalledWith('conv-1', 'slides/deck.pptx', 'original');
    expect(mocks.saveBlobToDisk).toHaveBeenCalledWith({ url: 'blob:2', filename: 'deck.pptx' });
  });

  it('fetches the sanitized copy of an image when the user picks it', async () => {
    mocks.downloadFile.mockResolvedValue({ url: 'blob:3', filename: 'chart.png' });
    const results: boolean[] = [];
    render(
      <DownloadWarningProvider>
        <DownloadProbe path="charts/chart.png" results={results} />
      </DownloadWarningProvider>,
    );
    fireEvent.click(screen.getByRole('button', { name: 'Download' }));
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Download Sanitized Copy' }));
    });
    await waitFor(() => expect(results).toEqual([true]));
    expect(mocks.downloadFile).toHaveBeenCalledWith('conv-1', 'charts/chart.png', 'sanitized');
    expect(mocks.saveBlobToDisk).toHaveBeenCalledWith({ url: 'blob:3', filename: 'chart.png' });
  });
});
