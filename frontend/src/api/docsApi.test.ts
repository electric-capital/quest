import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  docAssetBase,
  docAssetUrl,
  docDownloadUrl,
  fetchDocs,
  isStaleUpdateError,
  setDocMode,
  staleUpdateCurrent,
} from './docsApi';
import { ApiClientError, handleErrorResponse } from './request';

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

async function errorFrom(status: number, body: unknown): Promise<unknown> {
  try {
    await handleErrorResponse(jsonResponse(status, body));
  } catch (err) {
    return err;
  }
  throw new Error('handleErrorResponse did not throw');
}

describe('docsApi', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('recognizes the flat stale_update 409 and returns its current row', async () => {
    const current = { id: 'd1', title: 'Theirs', updated_at: '2026-10-02T00:00:00' };
    const err = await errorFrom(409, { error: 'stale_update', message: 'Changed', current });
    expect(isStaleUpdateError(err)).toBe(true);
    expect(staleUpdateCurrent(err)).toEqual(current);
  });

  it('does not treat other errors as stale updates', async () => {
    const duplicate = await errorFrom(409, {
      detail: { error: 'duplicate_title', message: 'Taken' },
    });
    expect((duplicate as ApiClientError).errorCode).toBe('duplicate_title');
    expect(isStaleUpdateError(duplicate)).toBe(false);
    expect(staleUpdateCurrent(duplicate)).toBeNull();
    expect(isStaleUpdateError(new Error('stale_update'))).toBe(false);
    expect(staleUpdateCurrent(new ApiClientError('x', 409, 'stale_update'))).toBeNull();
  });

  it('builds the cookie-authed asset and download URLs', () => {
    expect(docAssetBase('d1')).toBe('/app/api/docs/d1/assets');
    expect(docAssetUrl('d1', 'chart 1.png')).toBe('/app/api/docs/d1/assets/chart%201.png');
    expect(docDownloadUrl('d1', 'md')).toBe('/app/api/docs/d1/download?format=md');
    expect(docDownloadUrl('d1', 'zip')).toBe('/app/api/docs/d1/download?format=zip');
  });

  it('maps list options onto the query string', async () => {
    const fetchMock = vi.fn(async () => jsonResponse(200, { docs: [], has_more: false, next_cursor: null }));
    vi.stubGlobal('fetch', fetchMock);

    await fetchDocs();
    await fetchDocs({ projectId: 'p1', limit: 5, cursor: '2026-10-01T00:00:00|d9' });
    await setDocMode('d1', 'public');

    const calls = fetchMock.mock.calls as unknown as [string, RequestInit][];
    expect(calls[0][0]).toBe('/app/api/docs');
    expect(calls[1][0]).toBe(
      '/app/api/docs?project_id=p1&limit=5&cursor=2026-10-01T00%3A00%3A00%7Cd9',
    );
    expect(calls[2][0]).toBe('/app/api/docs/d1/mode');
    expect(calls[2][1]).toMatchObject({ method: 'PUT', body: JSON.stringify({ mode: 'public' }) });
  });
});
