// URL building of the source-parameterised file API (conversation space vs
// project space and the project twins) and the copy / move routes between a
// project conversation's two spaces, incl. the typed 409 destination_exists.
import { afterEach, beforeEach, describe, expect, it, onTestFinished, vi } from 'vitest';
import {
  FileApiError,
  conversationSource,
  copyFileFromProject,
  copyFileToProject,
  createFolder,
  createProjectFolder,
  deleteProjectFile,
  downloadProjectFile,
  downloadProjectFolder,
  fileDownloadUrl,
  fileRoutesBase,
  fileSourceKey,
  getProjectFileContent,
  getProjectFileInfo,
  isDestinationExistsError,
  listFiles,
  listProjectFiles,
  projectFileDownloadUrl,
  projectSource,
  saveProjectFileToDrive,
  uploadProjectFiles,
} from './fileApi';

const ORIGIN = window.location.origin;

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

let fetchMock: ReturnType<typeof vi.fn>;

/** The [url, init] of the n-th fetch call. */
function call(n = 0): { url: string; init: RequestInit } {
  const [url, init] = fetchMock.mock.calls[n] as [string, RequestInit];
  return { url, init };
}

beforeEach(() => {
  fetchMock = vi.fn(async () => jsonResponse({}));
  vi.stubGlobal('fetch', fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('file sources', () => {
  it('builds per-space route bases and keys', () => {
    expect(fileRoutesBase(conversationSource('c1'))).toBe('/app/api/conversations/c1/files');
    expect(fileRoutesBase('c1')).toBe('/app/api/conversations/c1/files');
    expect(fileRoutesBase(projectSource('p1'))).toBe('/app/api/projects/p1/files');
    expect(fileSourceKey(conversationSource('x'))).toBe('conversation:x');
    expect(fileSourceKey(projectSource('x'))).toBe('project:x');
  });

  it('builds download URLs for inline previews', () => {
    expect(fileDownloadUrl('c1', 'a b/c.png')).toBe('/app/api/conversations/c1/files/download?path=a%20b%2Fc.png');
    expect(projectFileDownloadUrl('p1', '/x.pdf')).toBe('/app/api/projects/p1/files/download?path=%2Fx.pdf');
  });
});

describe('project twins', () => {
  it('lists through the project routes, conversation ids keep the conversation routes', async () => {
    await listProjectFiles('p1', '/docs');
    await listFiles('c1', '');
    expect(call(0).url).toBe(`${ORIGIN}/app/api/projects/p1/files?path=%2Fdocs`);
    expect(call(0).init.method).toBe('GET');
    expect(call(1).url).toBe(`${ORIGIN}/app/api/conversations/c1/files`);
  });

  it('reads content and info', async () => {
    await getProjectFileContent('p1', '/a.md');
    await getProjectFileInfo('p1', '/dir');
    expect(call(0).url).toBe(`${ORIGIN}/app/api/projects/p1/files/content?path=%2Fa.md`);
    expect(call(1).url).toBe(`${ORIGIN}/app/api/projects/p1/files/info?path=%2Fdir`);
  });

  it('deletes, creates folders and saves to Drive', async () => {
    await deleteProjectFile('p1', '/old.txt');
    await createProjectFolder('p1', '/', 'new');
    await saveProjectFileToDrive('p1', '/a.md', 'A');
    await createFolder(conversationSource('c1'), '/', 'n');

    expect(call(0).url).toBe(`${ORIGIN}/app/api/projects/p1/files?path=%2Fold.txt`);
    expect(call(0).init.method).toBe('DELETE');
    expect(call(1).url).toBe(`${ORIGIN}/app/api/projects/p1/files/create-folder`);
    expect(JSON.parse(call(1).init.body as string)).toEqual({ path: '/', name: 'new' });
    expect(call(2).url).toBe(`${ORIGIN}/app/api/projects/p1/files/save-to-drive`);
    expect(JSON.parse(call(2).init.body as string)).toEqual({ path: '/a.md', title: 'A' });
    expect(call(3).url).toBe(`${ORIGIN}/app/api/conversations/c1/files/create-folder`);
  });

  it('downloads files and folders as blobs with the server filename', async () => {
    const original = URL.createObjectURL;
    URL.createObjectURL = vi.fn(() => 'blob:x');
    onTestFinished(() => { URL.createObjectURL = original; });
    fetchMock.mockResolvedValueOnce(new Response('data', {
      headers: { 'Content-Disposition': 'attachment; filename="report.csv"' },
    }));
    fetchMock.mockResolvedValueOnce(new Response('zip'));

    expect(await downloadProjectFile('p1', '/r.csv')).toEqual({ url: 'blob:x', filename: 'report.csv' });
    expect(await downloadProjectFolder('p1', '/out')).toEqual({ url: 'blob:x', filename: 'out.zip' });
    expect(call(0).url).toBe(`${ORIGIN}/app/api/projects/p1/files/download?path=%2Fr.csv`);
    expect(call(1).url).toBe(`${ORIGIN}/app/api/projects/p1/files/download-folder?path=%2Fout`);
  });

  it('uploads to the project upload route', async () => {
    const opened: string[] = [];
    class FakeXhr {
      status = 200;
      statusText = 'OK';
      responseText = JSON.stringify({ uploadedFiles: [], errors: [] });
      withCredentials = false;
      upload: { onprogress: unknown } = { onprogress: null };
      onload: (() => void) | null = null;
      onerror: (() => void) | null = null;
      open(_method: string, url: string) { opened.push(url); }
      send() { this.onload?.(); }
    }
    vi.stubGlobal('XMLHttpRequest', FakeXhr);

    await uploadProjectFiles('p1', [new File(['x'], 'x.txt')], '/in');
    expect(opened).toEqual([`${ORIGIN}/app/api/projects/p1/files/upload?path=%2Fin`]);
  });
});

describe('copy / move between spaces', () => {
  it('posts copy-to-project with the snake_case body and returns the moved flag', async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({
      type: 'file', path: '/out/a.csv', files_copied: 1, skipped: 0, moved: true,
    }));
    const result = await copyFileToProject('c1', {
      path: '/a.csv', dest: '/out/a.csv', overwrite: true, move: true, includeHidden: false,
    });
    expect(call().url).toBe(`${ORIGIN}/app/api/conversations/c1/files/copy-to-project`);
    expect(call().init.method).toBe('POST');
    expect(JSON.parse(call().init.body as string)).toEqual({
      path: '/a.csv', dest: '/out/a.csv', overwrite: true, move: true, include_hidden: false,
    });
    expect(result.moved).toBe(true);
  });

  it('posts copy-from-project with only the given fields', async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({
      type: 'folder', path: '/data', files_copied: 3, skipped: 1, moved: false,
    }));
    const result = await copyFileFromProject('c1', { path: '/data' });
    expect(call().url).toBe(`${ORIGIN}/app/api/conversations/c1/files/copy-from-project`);
    expect(JSON.parse(call().init.body as string)).toEqual({ path: '/data' });
    expect(result).toMatchObject({ type: 'folder', files_copied: 3, moved: false });
  });

  it('rejects a 409 destination_exists as a typed, detectable error', async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(
      { detail: { error: 'destination_exists', message: 'A file named a.csv already exists' } },
      409,
    ));
    const err = await copyFileToProject('c1', { path: '/a.csv' }).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(FileApiError);
    expect(isDestinationExistsError(err)).toBe(true);
    expect((err as FileApiError).statusCode).toBe(409);
    expect((err as FileApiError).errorCode).toBe('destination_exists');
    expect((err as FileApiError).message).toBe('A file named a.csv already exists');
  });

  it('does not treat other copy errors as destination_exists', async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(
      { detail: { error: 'forbidden_source', message: 'scratch' } },
      400,
    ));
    const err = await copyFileToProject('c1', { path: '/pasted/x.png' }).catch((e: unknown) => e);
    expect(isDestinationExistsError(err)).toBe(false);
    expect((err as FileApiError).errorCode).toBe('forbidden_source');
  });
});
