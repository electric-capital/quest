/**
 * API client for Quest Docs (/app/api/docs; see docs/api/quest-docs-api.md),
 * including a doc's notes (instructions and memories) and the review of
 * doc note suggestions (/app/api/doc-note-suggestions).
 *
 * Every JSON call goes through the shared request wrapper, so failures reject
 * with an ApiClientError carrying the server's error code. The download and
 * asset routes are cookie-authed GETs the browser fetches itself (`<a
 * download>`, `<img src>`), so only their URLs are built here.
 */

import { API_BASE_URL } from './config';
import {
  ApiClientError,
  apiDelete,
  apiGet,
  apiPost,
  apiPut,
  handleErrorResponse,
} from './request';
import type {
  AcceptDocNoteSuggestionRequest,
  AcceptDocNoteSuggestionResponse,
  CopyDocRevisionRequest,
  CreateDocRequest,
  DismissDocNoteSuggestionResponse,
  Doc,
  DocAssetDeleteResponse,
  DocAssetUploadResponse,
  DocContentResponse,
  DocDetail,
  DocMemory,
  DocNoteSuggestionsListResponse,
  DocNotes,
  DocRevisionDetail,
  DocRevisionsResponse,
  ListDocsResponse,
  RestoreDocRevisionRequest,
  SaveDocInstructionsRequest,
  ShareDocRequest,
  StaleInstructionsCurrent,
  UpdateDocContentRequest,
  UpdateDocRequest,
} from './types';

/** Feature-gate name; the UI shows docs only when `enabledFeatures` has it. */
export const DOCS_FEATURE = 'docs';

/**
 * Feature-gate name of doc notes (`enabledFeatures`). Without it the notes
 * routes answer 403 `doc_notes_disabled`; the suggestion routes are not gated.
 */
export const DOC_NOTES_FEATURE = 'doc_notes';

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
  /** List this project's docs; omitted / null = the user's own user docs. */
  projectId?: string | null;
  /**
   * List the docs other people shared with the viewer (user and project
   * docs, its own keyset stream). Not combinable with `projectId`.
   */
  shared?: boolean;
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
      shared: opts.shared ? 'true' : undefined,
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

// --- Sharing (owner only) --------------------------------------------------

/**
 * Grant (or change) read / write access for one user or everyone. Upsert:
 * one grant per recipient. Returns the owner's row with the new roster.
 * Rejects e.g. 404 `user_not_found`, 400 `cannot_share_with_owner`.
 */
export function shareDoc(id: string, body: ShareDocRequest): Promise<Doc> {
  return apiPost(`${docEndpoint(id)}/shares`, { body });
}

/** Revoke one grant. Returns the owner's row with the new roster. */
export function removeDocShare(id: string, shareId: number): Promise<Doc> {
  return apiDelete(`${docEndpoint(id)}/shares/${shareId}`);
}

// --- Human editing ---------------------------------------------------------

/**
 * Replace the body (owner or write share). A stale `expected_updated_at`
 * rejects with the flat 409 stale_update (isStaleUpdateError).
 */
export function updateDocContent(
  id: string,
  body: UpdateDocContentRequest,
): Promise<DocContentResponse> {
  return apiPut(`${docEndpoint(id)}/content`, { body });
}

/**
 * Upload one raster image into the doc's assets/ (owner or write share).
 * Multipart, so it bypasses the JSON wrapper but maps errors the same way.
 */
export async function uploadDocAsset(
  id: string,
  file: File | Blob,
  opts: { filename?: string; alt?: string } = {},
): Promise<DocAssetUploadResponse> {
  const form = new FormData();
  const filename = opts.filename ?? (file instanceof File ? file.name : 'image');
  form.append('file', file, filename);
  if (opts.alt !== undefined) form.append('alt', opts.alt);
  const response = await fetch(`${docAssetBase(id)}`, {
    method: 'POST',
    credentials: 'include',
    body: form,
  });
  if (!response.ok) {
    await handleErrorResponse(response);
  }
  return await response.json();
}

/** Delete one image (owner only); 409 `asset_in_use` while the body uses it. */
export function deleteDocAsset(id: string, name: string): Promise<DocAssetDeleteResponse> {
  return apiDelete(docAssetUrl(id, name));
}

// --- History ---------------------------------------------------------------

/** The current version plus every revision snapshot, newest first. */
export function fetchDocRevisions(id: string): Promise<DocRevisionsResponse> {
  return apiGet(`${docEndpoint(id)}/revisions`);
}

/** One revision's body; `diff: true` adds the revision -> current diff. */
export function fetchDocRevision(
  id: string,
  revisionId: string,
  opts: { diff?: boolean } = {},
): Promise<DocRevisionDetail> {
  return apiGet(`${docEndpoint(id)}/revisions/${encodeURIComponent(revisionId)}`, {
    query: { diff: opts.diff ? 'current' : undefined },
  });
}

/** Restore a revision (owner or write share); stale token -> 409 stale_update. */
export function restoreDocRevision(
  id: string,
  revisionId: string,
  body: RestoreDocRevisionRequest,
): Promise<DocContentResponse> {
  return apiPost(
    `${docEndpoint(id)}/revisions/${encodeURIComponent(revisionId)}/restore`,
    { body },
  );
}

/** Copy a revision into a NEW private doc of the viewer's own (201). */
export function copyDocRevision(
  id: string,
  revisionId: string,
  body: CopyDocRevisionRequest = {},
): Promise<Doc> {
  return apiPost(
    `${docEndpoint(id)}/revisions/${encodeURIComponent(revisionId)}/copy`,
    { body },
  );
}

// --- Notes (gated by DOC_NOTES_FEATURE: 403 doc_notes_disabled when off) ---

/**
 * The doc's instructions and memories (newest first) with the viewer's
 * rights. Rejects 404 `doc_not_found`, 403 `doc_notes_disabled`.
 */
export function fetchDocNotes(docId: string): Promise<DocNotes> {
  return apiGet(`${docEndpoint(docId)}/notes`);
}

/**
 * Replace the instructions (owner only; 403 `forbidden` otherwise) and
 * return the notes. Rejects 400 `invalid_instructions`; a stale
 * `expectedInstructionsVersion` rejects with the flat 409
 * stale_instructions (isStaleInstructionsError / staleInstructionsCurrent).
 */
export function saveDocInstructions(
  docId: string,
  instructions: string,
  expectedInstructionsVersion: number,
): Promise<DocNotes> {
  const body: SaveDocInstructionsRequest = {
    instructions,
    expected_instructions_version: expectedInstructionsVersion,
  };
  return apiPut(`${docEndpoint(docId)}/instructions`, { body });
}

/**
 * Add a memory (owner or write share; 201). Rejects 400 `invalid_memory`,
 * 409 `duplicate_memory` (duplicateMemoryId), 409 `memories_cap_reached`.
 */
export function addDocMemory(docId: string, content: string): Promise<DocMemory> {
  return apiPost(`${docEndpoint(docId)}/memories`, { body: { content } });
}

/**
 * Rewrite one memory (owner or write share). Rejects 404 `memory_not_found`,
 * 400 `invalid_memory`, 409 `duplicate_memory` (duplicateMemoryId).
 */
export function updateDocMemory(
  docId: string,
  memoryId: string,
  content: string,
): Promise<DocMemory> {
  return apiPut(`${docEndpoint(docId)}/memories/${encodeURIComponent(memoryId)}`, {
    body: { content },
  });
}

/** Delete one memory (owner or write share). Rejects 404 `memory_not_found`. */
export function deleteDocMemory(docId: string, memoryId: string): Promise<{ success: boolean }> {
  return apiDelete(`${docEndpoint(docId)}/memories/${encodeURIComponent(memoryId)}`);
}

// --- Note suggestions (never gated) ----------------------------------------

/** The viewer's own pending suggestions for a doc, oldest first; 404 `doc_not_found`. */
export function fetchDocNoteSuggestions(docId: string): Promise<DocNoteSuggestionsListResponse> {
  return apiGet(`${docEndpoint(docId)}/note-suggestions`);
}

/**
 * A conversation's pending doc note suggestions, oldest first. Rejects 404
 * `conversation_not_found`.
 */
export function fetchConversationDocNoteSuggestions(
  conversationId: string,
): Promise<DocNoteSuggestionsListResponse> {
  return apiGet(
    `${API_BASE_URL}/conversations/${encodeURIComponent(conversationId)}/doc-note-suggestions`,
  );
}

/** `/app/api/doc-note-suggestions/{id}` (accept, dismiss). */
function docNoteSuggestionEndpoint(suggestionId: string): string {
  return `${API_BASE_URL}/doc-note-suggestions/${encodeURIComponent(suggestionId)}`;
}

/**
 * Apply a suggestion to the doc's notes. `content` overrides the proposed
 * text; `expectedInstructionsVersion` is the version an instructions row's
 * diff was rendered against. Only the options given are sent (`{}` for
 * none). Rejects 404 `suggestion_not_found`, 409 `suggestion_resolved`, 403
 * `forbidden`, 400 `invalid_memory` / `invalid_instructions`, 409
 * `memories_cap_reached`, 409 `duplicate_memory` (duplicateMemoryId) and
 * the flat 409 stale_instructions (staleInstructionsCurrent).
 */
export function acceptDocNoteSuggestion(
  suggestionId: string,
  opts: { content?: string; expectedInstructionsVersion?: number } = {},
): Promise<AcceptDocNoteSuggestionResponse> {
  const body: AcceptDocNoteSuggestionRequest = {};
  if (opts.content !== undefined) body.content = opts.content;
  if (opts.expectedInstructionsVersion !== undefined) {
    body.expected_instructions_version = opts.expectedInstructionsVersion;
  }
  return apiPost(`${docNoteSuggestionEndpoint(suggestionId)}/accept`, { body });
}

/**
 * Resolve a suggestion without a change (no edit access needed). Rejects
 * 404 `suggestion_not_found`, 409 `suggestion_resolved`.
 */
export function dismissDocNoteSuggestion(
  suggestionId: string,
): Promise<DismissDocNoteSuggestionResponse> {
  return apiPost(`${docNoteSuggestionEndpoint(suggestionId)}/dismiss`);
}

// --- Conflict helpers ------------------------------------------------------

/**
 * True for the optimistic-concurrency conflict of the rename, content-save
 * and restore endpoints. That 409
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

/**
 * True for the instructions conflict of saveDocInstructions and
 * acceptDocNoteSuggestion. Like stale_update, that 409 has a FLAT body
 * (`{error: 'stale_instructions', message, current}`).
 */
export function isStaleInstructionsError(err: unknown): err is ApiClientError {
  return err instanceof ApiClientError && err.errorCode === 'stale_instructions';
}

/** The server's current instructions from a stale_instructions error, else null. */
export function staleInstructionsCurrent(err: unknown): StaleInstructionsCurrent | null {
  if (!isStaleInstructionsError(err)) return null;
  const current = err.details?.current;
  if (!current || typeof current !== 'object') return null;
  const { instructions, instructions_version: version } = current as Record<string, unknown>;
  if (typeof instructions !== 'string' || typeof version !== 'number') return null;
  return { instructions, instructions_version: version };
}

/**
 * The id of the memory that already has the text, from a 409
 * duplicate_memory (`{detail: {error, message, memory_id}}`), else null.
 */
export function duplicateMemoryId(err: unknown): string | null {
  if (!(err instanceof ApiClientError) || err.errorCode !== 'duplicate_memory') return null;
  const detail = err.details?.detail;
  if (!detail || typeof detail !== 'object') return null;
  const memoryId = (detail as Record<string, unknown>).memory_id;
  return typeof memoryId === 'string' && memoryId ? memoryId : null;
}
