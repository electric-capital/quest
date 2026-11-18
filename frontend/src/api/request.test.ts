// The error accessors of the shared request wrapper: the server's error code
// and FastAPI's structured `detail` object, from both body shapes the routes
// return (flat `{error, message}` and `{detail: {error, message, ...}}`).
import { describe, expect, it } from 'vitest';
import { ApiClientError, apiErrorCode, apiErrorDetail, handleErrorResponse } from './request';

async function errorFrom(status: number, body: unknown): Promise<unknown> {
  const response = new Response(typeof body === 'string' ? body : JSON.stringify(body), {
    status,
    statusText: 'Bad',
    headers: { 'Content-Type': 'application/json' },
  });
  try {
    await handleErrorResponse(response);
  } catch (err) {
    return err;
  }
  throw new Error('handleErrorResponse did not throw');
}

describe('apiErrorCode', () => {
  it('reads the code of a FastAPI envelope and of a flat body', async () => {
    expect(apiErrorCode(await errorFrom(409, { detail: { error: 'stale_brief', message: 'm' } }))).toBe('stale_brief');
    expect(apiErrorCode(await errorFrom(400, { error: 'invalid_title', message: 'm' }))).toBe('invalid_title');
  });

  it('is null for a string detail, an unparsable body and non-API errors', async () => {
    expect(apiErrorCode(await errorFrom(404, { detail: 'Not Found' }))).toBeNull();
    expect(apiErrorCode(await errorFrom(500, 'not json'))).toBeNull();
    expect(apiErrorCode(new TypeError('Failed to fetch'))).toBeNull();
    expect(apiErrorCode('autoproject_not_found')).toBeNull();
    expect(apiErrorCode(null)).toBeNull();
  });
});

describe('apiErrorDetail', () => {
  it('returns the structured detail with its extras', async () => {
    const detail = {
      error: 'stale_brief',
      message: 'The brief changed',
      current: { brief: 'B', brief_version: 3 },
    };
    expect(apiErrorDetail(await errorFrom(409, { detail }))).toEqual(detail);
  });

  it('is null for flat bodies, string details and anything that is not an ApiClientError', async () => {
    expect(apiErrorDetail(await errorFrom(400, { error: 'x', message: 'y' }))).toBeNull();
    expect(apiErrorDetail(await errorFrom(404, { detail: 'Not Found' }))).toBeNull();
    expect(apiErrorDetail(new ApiClientError('m', 400, 'x'))).toBeNull();
    expect(apiErrorDetail(new Error('boom'))).toBeNull();
    expect(apiErrorDetail({ details: { detail: { error: 'forged' } } })).toBeNull();
  });
});
