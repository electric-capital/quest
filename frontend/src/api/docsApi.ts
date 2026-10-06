/**
 * API client for Quest Docs (/app/api/docs; see docs/api/quest-docs-api.md).
 *
 * Every JSON call goes through the shared request wrapper, so failures reject
 * with an ApiClientError carrying the server's error code. The download and
 * asset routes are cookie-authed GETs the browser fetches itself (`<a
 * download>`, `<img src>`), so only their URLs are built here.
 */

import { API_BASE_URL } from './config';
import { ApiClientError, apiDelete, apiGet, apiPost, apiPut } from './request';
import type {
  CreateDocRequest,
  Doc,
  DocDetail,
  ListDocsResponse,
  UpdateDocRequest,
} from './types';

/** Feature-gate name; the UI shows docs only when `enabledFeatures` has it. */
export const DOCS_FEATURE = 'docs';

/** Server-side cap on a list page (`limit` must be 1..200). */
export const DOCS_MAX_PAGE_SIZE = 200;

export type DocDownloadFormat = 'md' | 'zip';

/** `/app/api/docs` (list + create). */
export function docsEndpoint(): string {
  return `${API_BASE_URL}/docs`;
}

/** `/app/api/docs/{id}` (read, rename, delete). */
export function docEndpoint(id: string): string {
  return `${docsEndpoint()}/${encodeURIComponent(id)}`;
}

/** Base URL the doc's `assets/<name>` image references resolve against. */
export function docAssetBase(id: string): string {
  return `${docEndpoint(id)}/assets`;
}

/** Inline URL of one embedded image (plain `<img src>` works: cookie auth). */
export function docAssetUrl(id: string, name: string): string {
  return `${docAssetBase(id)}/${encodeURIComponent(name)}`;
}

/** Attachment URL: `md` = doc.md alone, `zip` = doc.md + assets/. */
export function docDownloadUrl(id: string, format: DocDownloadFormat): string {
  return `${docEndpoint(id)}/download?format=${format}`;
}

export interface FetchDocsOptions {
  /** List this project's docs; omitted / null = the user's own + shared docs. */
  projectId?: string | null;
  /** Page size (1..200); omitted = the whole list, unpaged. */
  limit?: number;
  /** Opaque `next_cursor` from the previous page. */
  cursor?: string | null;
}

/**
 * One page of docs, newest `updated_at` first. Rejects with
 * `project_not_found` (404) or `invalid_cursor` (400).
 */
export function fetchDocs(opts: FetchDocsOptions = {}): Promise<ListDocsResponse> {
  return apiGet(docsEndpoint(), {
    query: {
      project_id: opts.projectId || undefined,
      limit: opts.limit,
      cursor: opts.cursor || undefined,
    },
  });
}

/** Create an empty doc (201). Rejects with e.g. 409 `duplicate_title`. */
export function createDoc(body: CreateDocRequest): Promise<Doc> {
  return apiPost(docsEndpoint(), { body });
}

/** The row plus the whole markdown body. Rejects 404 `doc_not_found`. */
export function fetchDoc(id: string): Promise<DocDetail> {
  return apiGet(docEndpoint(id));
}

/**
 * Rename and/or re-describe (owner only). Returns the row WITHOUT `content`.
 * A stale `expected_updated_at` rejects with a 409 stale_update error whose
 * body carries the current row: see isStaleUpdateError / staleUpdateCurrent.
 */
export function updateDoc(id: string, body: UpdateDocRequest): Promise<Doc> {
  return apiPut(docEndpoint(id), { body });
}

/** Delete a doc and its files (owner only). */
export function deleteDoc(id: string): Promise<{ deleted: boolean }> {
  return apiDelete(docEndpoint(id));
}

/**
 * True for the rename endpoint's optimistic-concurrency conflict. That 409
 * has a FLAT body (`{error: 'stale_update', message, current}`), which
 * request.ts maps to `errorCode` and keeps whole in `details`.
 */
export function isStaleUpdateError(err: unknown): err is ApiClientError {
  return err instanceof ApiClientError && err.errorCode === 'stale_update';
}

/** The server's current row from a stale_update error, else null. */
export function staleUpdateCurrent(err: unknown): Doc | null {
  if (!isStaleUpdateError(err)) return null;
  const current = err.details?.current;
  return current && typeof current === 'object' ? (current as Doc) : null;
}
