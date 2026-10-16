/**
 * Bounded automatic retry for the read requests a view makes as it opens.
 *
 * A viewer's first fetch can fail for reasons that clear on their own
 * within a moment -- a dropped connection ("Failed to fetch"), a proxy
 * returning 502 / 504 while the backend is busy, the backend answering 503
 * or 429 -- and showing a Retry button for those sends the user to do by
 * hand what one more request would have done. `withTransientRetry` runs the
 * request again after each `TRANSIENT_RETRY_DELAYS_MS` entry (two more
 * tries over about 1.6 seconds) while the failure looks transient, then
 * rethrows the last error for the caller's own error state.
 *
 * A client-side rejection -- a 4xx other than 408 / 425 / 429 -- is never
 * retried: a 404 says the table is gone, a 403 that access is denied, a
 * 400 that the request itself is wrong, and repeating those only delays the
 * message. `stillWanted` lets a caller that supersedes requests (a newer
 * fetch, an unmounted hook) stop the retries early; the error is rethrown
 * at once then, and the caller drops it as it would any stale response.
 */

import { ApiClientError } from '../api/request';

/** Pauses before the second and third attempt. */
export const TRANSIENT_RETRY_DELAYS_MS: readonly number[] = [400, 1200];

const TRANSIENT_STATUSES = new Set([408, 425, 429, 500, 502, 503, 504]);

/**
 * Whether a failed request is worth repeating: a network-level failure
 * (fetch rejected, so no ApiClientError and no status), or a status in
 * `TRANSIENT_STATUSES`.
 */
export function isTransientError(err: unknown): boolean {
  if (!(err instanceof ApiClientError)) return true;
  return err.statusCode !== undefined && TRANSIENT_STATUSES.has(err.statusCode);
}

export interface TransientRetryOptions {
  /** Checked after each pause; false stops retrying (the error is rethrown). */
  stillWanted?: () => boolean;
  /** Pauses before each retry; the length is the number of retries. */
  delays?: readonly number[];
}

/**
 * Run `request`, repeating it after each delay while it fails with a
 * transient error. Resolves with the first success, rejects with the last
 * error.
 */
export async function withTransientRetry<T>(
  request: () => Promise<T>,
  options: TransientRetryOptions = {},
): Promise<T> {
  const delays = options.delays ?? TRANSIENT_RETRY_DELAYS_MS;
  for (let attempt = 0; ; attempt++) {
    try {
      return await request();
    } catch (err) {
      if (attempt >= delays.length || !isTransientError(err)) throw err;
      await new Promise<void>((resolve) => setTimeout(resolve, delays[attempt]));
      if (options.stillWanted && !options.stillWanted()) throw err;
    }
  }
}
