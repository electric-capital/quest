import { describe, expect, it, vi } from 'vitest';
import { ApiClientError } from '../api/request';
import { isTransientError, withTransientRetry } from './transientRetry';

describe('isTransientError', () => {
  it('treats network failures and gateway / busy statuses as transient', () => {
    expect(isTransientError(new TypeError('Failed to fetch'))).toBe(true);
    expect(isTransientError(new ApiClientError('no status'))).toBe(false);
    for (const status of [408, 425, 429, 500, 502, 503, 504]) {
      expect(isTransientError(new ApiClientError('x', status))).toBe(true);
    }
    for (const status of [400, 401, 403, 404, 409, 422]) {
      expect(isTransientError(new ApiClientError('x', status))).toBe(false);
    }
  });
});

describe('withTransientRetry', () => {
  it('retries a transient failure and resolves with the first success', async () => {
    const request = vi
      .fn<() => Promise<string>>()
      .mockRejectedValueOnce(new ApiClientError('busy', 503))
      .mockRejectedValueOnce(new TypeError('Failed to fetch'))
      .mockResolvedValueOnce('ok');
    await expect(withTransientRetry(request, { delays: [1, 1] })).resolves.toBe('ok');
    expect(request).toHaveBeenCalledTimes(3);
  });

  it('rethrows the last error once the retries are used up', async () => {
    const last = new ApiClientError('still busy', 503);
    const request = vi
      .fn<() => Promise<string>>()
      .mockRejectedValueOnce(new ApiClientError('busy', 503))
      .mockRejectedValueOnce(last);
    await expect(withTransientRetry(request, { delays: [1] })).rejects.toBe(last);
    expect(request).toHaveBeenCalledTimes(2);
  });

  it('does not retry a client-side rejection', async () => {
    const gone = new ApiClientError('gone', 404);
    const request = vi.fn<() => Promise<string>>().mockRejectedValue(gone);
    await expect(withTransientRetry(request, { delays: [1, 1] })).rejects.toBe(gone);
    expect(request).toHaveBeenCalledTimes(1);
  });

  it('stops after the pause when the result is no longer wanted', async () => {
    const busy = new ApiClientError('busy', 503);
    const request = vi.fn<() => Promise<string>>().mockRejectedValue(busy);
    let wanted = true;
    const promise = withTransientRetry(request, { delays: [1, 1], stillWanted: () => wanted });
    wanted = false;
    await expect(promise).rejects.toBe(busy);
    expect(request).toHaveBeenCalledTimes(1);
  });
});
