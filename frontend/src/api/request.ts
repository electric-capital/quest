/**
 * Shared JSON request wrapper for the API client modules.
 *
 * Every client function is "fetch this URL with the session cookie, throw an
 * ApiClientError on a non-2xx status, return the parsed JSON body". This
 * module holds that one shape so the per-endpoint functions in client.ts are
 * a single call each.
 */

/**
 * Custom error class for API client errors
 */
export class ApiClientError extends Error {
  statusCode?: number;
  errorCode?: string;
  stacktrace?: string;
  // Full parsed error body. Callers that need typed fields beyond the
  // common message/error/stacktrace shape (e.g. the `current` row payload
  // on a 409 stale_update response) can pull them from here.
  details?: Record<string, unknown>;

  constructor(
    message: string,
    statusCode?: number,
    errorCode?: string,
    stacktrace?: string,
    details?: Record<string, unknown>,
  ) {
    super(message);
    this.name = 'ApiClientError';
    this.statusCode = statusCode;
    this.errorCode = errorCode;
    this.stacktrace = stacktrace;
    this.details = details;
  }
}

/**
 * A failed request as plain fields for a console log line: an
 * ApiClientError's status, code and parsed body, or the name and message
 * of anything else (a network failure is a TypeError "Failed to fetch"
 * with no status). `online` is the browser's own view of connectivity at
 * the time.
 */
export function describeRequestError(err: unknown): Record<string, unknown> {
  const base = {
    name: err instanceof Error ? err.name : typeof err,
    message: err instanceof Error ? err.message : String(err),
    online: typeof navigator === 'undefined' ? undefined : navigator.onLine,
  };
  if (!(err instanceof ApiClientError)) return base;
  return { ...base, status: err.statusCode, code: err.errorCode, details: err.details };
}

type QueryValue = string | number | boolean | null | undefined;

/** Query-string params; `undefined` and `null` entries are omitted. */
export type QueryParams = Record<string, QueryValue>;

/**
 * Append query params to a URL, skipping undefined/null values. Booleans and
 * numbers are stringified, so `{ limit: 30, include_slack: false }` becomes
 * `?limit=30&include_slack=false`.
 */
export function withQuery(url: string, params?: QueryParams): string {
  if (!params) return url;
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null) continue;
    search.set(key, String(value));
  }
  const query = search.toString();
  return query ? `${url}?${query}` : url;
}

/**
 * Turn a non-2xx response into an ApiClientError.
 *
 * Error bodies arrive in two shapes: the flat `{error, message}` JSON the
 * app's own routes return, and FastAPI's HTTPException envelope --
 * `{detail: {error, message}}` for structured details or `{detail: "text"}`
 * for string ones. The human message and error code are pulled out of
 * whichever shape arrived; `details` keeps the whole parsed body.
 */
export async function handleErrorResponse(response: Response): Promise<never> {
  let errorData: Record<string, unknown> | null = null;

  try {
    errorData = await response.json();
  } catch {
    // If we can't parse JSON, use status text
    throw new ApiClientError(
      response.statusText || 'Unknown error',
      response.status,
    );
  }

  const detail = errorData?.detail;
  const detailObj =
    detail && typeof detail === 'object' ? (detail as Record<string, unknown>) : undefined;
  const message =
    (errorData?.message as string | undefined) ||
    (typeof detail === 'string' ? detail : (detailObj?.message as string | undefined)) ||
    'Unknown error';
  const errorCode =
    (errorData?.error as string | undefined) ?? (detailObj?.error as string | undefined);
  const stacktrace =
    (errorData?.stacktrace as string | undefined) ??
    (detailObj?.stacktrace as string | undefined);

  throw new ApiClientError(
    message,
    response.status,
    errorCode,
    stacktrace,
    errorData ?? undefined,
  );
}

export interface RequestOptions {
  /** Query-string params appended to the URL (undefined/null entries dropped). */
  query?: QueryParams;
  /** JSON-serialized request body; sets the Content-Type header when present. */
  body?: unknown;
  /**
   * Statuses that resolve to `null` instead of throwing, for endpoints where
   * e.g. a 404 means "no row yet" rather than an error.
   */
  nullOn?: number[];
}

type HttpMethod = 'GET' | 'POST' | 'PUT' | 'DELETE';

/**
 * Perform a session-cookie-authed JSON request and return the parsed body.
 */
export async function apiRequest<T>(
  method: HttpMethod,
  url: string,
  options: RequestOptions = {},
): Promise<T> {
  const init: RequestInit = { method, credentials: 'include' };
  if (options.body !== undefined) {
    init.headers = { 'Content-Type': 'application/json' };
    init.body = JSON.stringify(options.body);
  }

  const response = await fetch(withQuery(url, options.query), init);

  if (options.nullOn?.includes(response.status)) {
    return null as T;
  }
  if (!response.ok) {
    await handleErrorResponse(response);
  }

  return await response.json();
}

export function apiGet<T>(url: string, options?: RequestOptions): Promise<T> {
  return apiRequest<T>('GET', url, options);
}

export function apiPost<T>(url: string, options?: RequestOptions): Promise<T> {
  return apiRequest<T>('POST', url, options);
}

export function apiPut<T>(url: string, options?: RequestOptions): Promise<T> {
  return apiRequest<T>('PUT', url, options);
}

export function apiDelete<T>(url: string, options?: RequestOptions): Promise<T> {
  return apiRequest<T>('DELETE', url, options);
}
