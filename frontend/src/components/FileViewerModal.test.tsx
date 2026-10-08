// FileViewerModal reads from its file space: a project source fetches text
// content, images and Save to Drive through the project routes, while the
// legacy `conversationId` prop keeps the conversation routes.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { FileViewerModal } from './FileViewerModal';

// pdf.js needs browser canvas APIs jsdom lacks.
vi.mock('./PdfViewer', () => ({ PdfViewer: () => null }));

const ORIGIN = window.location.origin;
let fetchMock: ReturnType<typeof vi.fn>;
let originalCreateObjectURL: typeof URL.createObjectURL;

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } });
}

const fetchedUrls = () => fetchMock.mock.calls.map(([url]) => String(url));

beforeEach(() => {
  fetchMock = vi.fn(async (url: string) => {
    if (String(url).includes('/files/content')) {
      return jsonResponse({ name: 'notes.md', path: '/notes.md', content: '# Notes', size: 7 });
    }
    if (String(url).includes('/files/save-to-drive')) {
      return jsonResponse({ id: 'g1', name: 'notes', url: 'https://docs.example/g1' });
    }
    return new Response(new Blob(['img']));
  });
  vi.stubGlobal('fetch', fetchMock);
  originalCreateObjectURL = URL.createObjectURL;
  URL.createObjectURL = vi.fn(() => 'blob:img');
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  URL.createObjectURL = originalCreateObjectURL;
});

const noop = () => {};

describe('FileViewerModal with a project source', () => {
  it('loads text content and saves to Drive through the project routes', async () => {
    render(
      <FileViewerModal
        isOpen
        source={{ kind: 'project', id: 'p1' }}
        filePath="/notes.md"
        fileName="notes.md"
        onClose={noop}
        onDownload={noop}
      />,
    );
    await screen.findByRole('heading', { name: 'Notes' });
    expect(fetchedUrls()).toEqual([`${ORIGIN}/app/api/projects/p1/files/content?path=%2Fnotes.md`]);

    fireEvent.click(screen.getByRole('button', { name: 'Save to Drive' }));
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    await screen.findByText('Saved successfully!');
    const [url, init] = fetchMock.mock.calls[1] as [string, RequestInit];
    expect(url).toBe(`${ORIGIN}/app/api/projects/p1/files/save-to-drive`);
    expect(init.method).toBe('POST');
    expect(JSON.parse(init.body as string)).toEqual({ path: '/notes.md', title: 'notes' });
  });

  it('fetches an image from the project download route', async () => {
    render(
      <FileViewerModal
        isOpen
        source={{ kind: 'project', id: 'p1' }}
        filePath="/charts/a.png"
        fileName="a.png"
        isImage
        onClose={noop}
        onDownload={noop}
      />,
    );
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    expect(fetchedUrls()).toEqual(['/app/api/projects/p1/files/download?path=%2Fcharts%2Fa.png']);
  });
});

describe('FileViewerModal with a conversationId', () => {
  it('keeps the conversation routes', async () => {
    render(
      <FileViewerModal
        isOpen
        conversationId="c1"
        filePath="/charts/a.png"
        fileName="a.png"
        isImage
        onClose={noop}
        onDownload={noop}
      />,
    );
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    expect(fetchedUrls()).toEqual(['/app/api/conversations/c1/files/download?path=%2Fcharts%2Fa.png']);
  });
});
