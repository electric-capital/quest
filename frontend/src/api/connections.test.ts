// Multi-connection client helpers (createConnection / updateConnection /
// deleteConnection): URLs and bodies against the row's connections_url, and
// the structured ConnectionApiError parsed from the routes' error bodies.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  ConnectionApiError,
  createConnection,
  deleteConnection,
  updateConnection,
} from './client';
import { ApiClientError } from './request';

const URL_BASE = '/auth/connections/imap';
const ROW = {
  id: 7, account: 'me@example.com', description: 'work', is_default: true,
  needs_reauth: false, warning: null, created_at: '2026-10-01T00:00:00',
};

function jsonResponse(body: unknown, status = 200, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json', ...headers },
  });
}

let fetchMock: ReturnType<typeof vi.fn>;

function call(n = 0): { url: string; init: RequestInit } {
  const [url, init] = fetchMock.mock.calls[n] as [string, RequestInit];
  return { url, init };
}

async function caught(promise: Promise<unknown>): Promise<ConnectionApiError> {
  try {
    await promise;
  } catch (error) {
    expect(error).toBeInstanceOf(ConnectionApiError);
    expect(error).toBeInstanceOf(ApiClientError);
    return error as ConnectionApiError;
  }
  throw new Error('expected a rejection');
}

beforeEach(() => {
  fetchMock = vi.fn(async () => jsonResponse({ connection: ROW }));
  vi.stubGlobal('fetch', fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('connection helpers', () => {
  it('creates with a JSON body and returns the connection', async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ connection: ROW }, 201));
    const row = await createConnection(URL_BASE, {
      values: { host: 'imap.example.com', username: 'me@example.com', password: 'pw' },
      description: 'work',
    });
    expect(row).toEqual(ROW);
    expect(call().url).toBe(URL_BASE);
    expect(call().init.method).toBe('POST');
    expect(call().init.credentials).toBe('include');
    expect((call().init.headers as Record<string, string>)['Content-Type']).toBe('application/json');
    expect(JSON.parse(call().init.body as string)).toEqual({
      values: { host: 'imap.example.com', username: 'me@example.com', password: 'pw' },
      description: 'work',
    });
  });

  it('creates an api_key connection', async () => {
    await createConnection(URL_BASE, { api_key: 'k-1' });
    expect(JSON.parse(call().init.body as string)).toEqual({ api_key: 'k-1' });
  });

  it('updates <connections_url>/<id>', async () => {
    const row = await updateConnection(`${URL_BASE}/`, 7, { is_default: true, description: null });
    expect(row).toEqual(ROW);
    expect(call().url).toBe(`${URL_BASE}/7`);
    expect(call().init.method).toBe('PUT');
    expect(JSON.parse(call().init.body as string)).toEqual({ is_default: true, description: null });
  });

  it('deletes and accepts the empty 204', async () => {
    fetchMock.mockResolvedValueOnce(new Response(null, { status: 204 }));
    await expect(deleteConnection(URL_BASE, 7)).resolves.toBeUndefined();
    expect(call().url).toBe(`${URL_BASE}/7`);
    expect(call().init.method).toBe('DELETE');
    expect(call().init.body).toBeUndefined();
  });
});

describe('ConnectionApiError parsing', () => {
  it('carries the invalid_values field map', async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({
      detail: {
        error: 'invalid_values', message: 'Some fields are invalid.',
        fields: { host: 'Not one of the allowed choices.', password: 'This field is required.' },
      },
    }, 400));
    const error = await caught(createConnection(URL_BASE, { values: {} }));
    expect(error.status).toBe(400);
    expect(error.statusCode).toBe(400);
    expect(error.code).toBe('invalid_values');
    expect(error.errorCode).toBe('invalid_values');
    expect(error.message).toBe('Some fields are invalid.');
    expect(error.fields).toEqual({
      host: 'Not one of the allowed choices.', password: 'This field is required.',
    });
    expect(error.connectionId).toBeUndefined();
  });

  it('carries the verify_failed message', async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({
      detail: { error: 'verify_failed', message: 'LOGIN failed: bad password' },
    }, 400));
    const error = await caught(createConnection(URL_BASE, { values: {} }));
    expect(error.code).toBe('verify_failed');
    expect(error.message).toBe('LOGIN failed: bad password');
    expect(error.fields).toBeUndefined();
  });

  it('carries the existing connection on connection_exists', async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({
      detail: {
        error: 'connection_exists', message: 'me@example.com is already connected.',
        account: 'me@example.com', connection_id: 7,
      },
    }, 409));
    const error = await caught(createConnection(URL_BASE, { values: {} }));
    expect(error.status).toBe(409);
    expect(error.code).toBe('connection_exists');
    expect(error.account).toBe('me@example.com');
    expect(error.connectionId).toBe(7);
  });

  it('keeps a null connection_id as null', async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({
      detail: { error: 'connection_exists', message: 'x', account: 'a@b.c', connection_id: null },
    }, 409));
    expect((await caught(createConnection(URL_BASE, {}))).connectionId).toBeNull();
  });

  it('carries the limit on connection_limit', async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({
      detail: { error: 'connection_limit', message: 'At most 10 IMAP connections are allowed.', limit: 10 },
    }, 400));
    const error = await caught(createConnection(URL_BASE, {}));
    expect(error.code).toBe('connection_limit');
    expect(error.limit).toBe(10);
  });

  it('reads Retry-After on 429', async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(
      { detail: { error: 'rate_limited', message: 'Too many connection attempts.', retry_after: 30 } },
      429,
      { 'Retry-After': '42' },
    ));
    const error = await caught(updateConnection(URL_BASE, 7, { values: {} }));
    expect(error.status).toBe(429);
    expect(error.code).toBe('rate_limited');
    expect(error.retryAfter).toBe(42);
  });

  it('falls back to the body retry_after without the header', async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(
      { detail: { error: 'rate_limited', message: 'Too many.', retry_after: 30 } },
      429,
    ));
    expect((await caught(createConnection(URL_BASE, {}))).retryAfter).toBe(30);
  });

  it('handles account_mismatch and cannot_unset_default codes', async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({
      detail: { error: 'account_mismatch', message: 'These settings belong to x, not y.' },
    }, 400));
    const mismatch = await caught(updateConnection(URL_BASE, 7, { values: {} }));
    expect(mismatch.code).toBe('account_mismatch');
    expect(mismatch.message).toBe('These settings belong to x, not y.');
  });

  it('handles non-JSON and string-detail bodies', async () => {
    fetchMock.mockResolvedValueOnce(new Response('oops', { status: 502, statusText: 'Bad Gateway' }));
    const gateway = await caught(deleteConnection(URL_BASE, 7));
    expect(gateway.status).toBe(502);
    expect(gateway.message).toBe('Bad Gateway');
    expect(gateway.code).toBeUndefined();

    fetchMock.mockResolvedValueOnce(jsonResponse({ detail: 'Not authenticated' }, 401));
    const unauth = await caught(deleteConnection(URL_BASE, 7));
    expect(unauth.status).toBe(401);
    expect(unauth.message).toBe('Not authenticated');
  });

  it('reads a FastAPI 422 validation list as invalid_request', async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse(
        { detail: [{ loc: ['path', 'connection_id'], msg: 'Input should be a valid integer', type: 'int_parsing' }] },
        422,
      ),
    );
    const error = await caught(updateConnection(URL_BASE, 7, { description: 'x' }));
    expect(error.status).toBe(422);
    expect(error.code).toBe('invalid_request');
    expect(error.message).toBe('Input should be a valid integer');
    expect(error.fields).toBeUndefined();
    expect(error.connectionId).toBeUndefined();
  });

  it('wraps network failures as status 0 network_error', async () => {
    fetchMock.mockRejectedValueOnce(new TypeError('Failed to fetch'));
    const error = await caught(createConnection(URL_BASE, {}));
    expect(error.status).toBe(0);
    expect(error.code).toBe('network_error');
    expect(error.message).toBe('Failed to fetch');
  });
});
