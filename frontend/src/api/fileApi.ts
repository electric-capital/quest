/**
 * API client for workspace file management.
 *
 * Two file spaces share one route set (chat/file_routes.py): a
 * conversation's workspace (`/conversations/{cid}/files...`, the "Chat
 * Files" card, and every inline-image / attachment path in chat) and a
 * project's shared workspace (`/projects/{pid}/files...`, the "Project
 * Files" card). Every function takes a `FileTarget`: a `FileSource`, or a
 * bare string meaning a conversation id -- the historical signature the
 * many conversation-space callers (Composer, HomeComposer, MarkdownImage,
 * previews) keep using. The `*Project*` twins are thin wrappers over a
 * project source. `copyFileToProject` / `copyFileFromProject` move entries
 * between the two spaces of a project conversation.
 */

import { API_BASE_URL } from './config';
import type {
  ListFilesResponse,
  UploadResponse,
  FileContentResponse,
  FileInfoResponse,
  DeleteFileResponse,
  CreateFolderResponse,
  CopyEntryResponse,
  ApiError,
  ComposerAttachmentUploadResponse,
} from './types';
import type { FileWithPath } from '../utils/directoryTraversal';

/**
 * Custom error class for file API errors
 */
export class FileApiError extends Error {
  statusCode?: number;
  errorCode?: string;

  constructor(message: string, statusCode?: number, errorCode?: string) {
    super(message);
    this.name = 'FileApiError';
    this.statusCode = statusCode;
    this.errorCode = errorCode;
  }
}

/**
 * Handle error responses from the API
 */
async function handleErrorResponse(response: Response): Promise<never> {
  let errorData: unknown = null;

  try {
    errorData = await response.json();
  } catch {
    throw new FileApiError(
      response.statusText || 'Unknown error',
      response.status
    );
  }

  // Normalize error shapes. File routes raise FastAPI HTTPException with a
  // structured detail (`{detail: {error, message}}`); FastAPI also emits a
  // bare `{detail: "text"}` for string details. A few endpoints return a flat
  // `{error, message}`. Pull the human message and code out of whichever shape
  // arrived so the modal shows the real reason instead of "Unknown error".
  const body = (errorData && typeof errorData === 'object' ? errorData : {}) as {
    detail?: unknown;
    message?: unknown;
    error?: unknown;
  };
  const detail = body.detail;
  const detailObj = detail && typeof detail === 'object'
    ? (detail as { message?: unknown; error?: unknown })
    : undefined;
  const message =
    (typeof body.message === 'string' ? body.message : undefined) ??
    (typeof detail === 'string' ? detail : undefined) ??
    (typeof detailObj?.message === 'string' ? detailObj.message : undefined) ??
    'Unknown error';
  const rawCode = body.error ?? detailObj?.error;
  const errorCode = typeof rawCode === 'string' ? rawCode : undefined;

  throw new FileApiError(message, response.status, errorCode);
}

/**
 * Perform a file upload via XMLHttpRequest with progress tracking.
 * Uses XHR instead of fetch() because fetch does not support upload progress events.
 */
function xhrUpload(
  url: string,
  formData: FormData,
  onProgress?: (loaded: number, total: number) => void
): Promise<UploadResponse> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', url);
    xhr.withCredentials = true;

    if (onProgress) {
      xhr.upload.onprogress = (event) => {
        if (event.lengthComputable) {
          onProgress(event.loaded, event.total);
        }
      };
    }

    xhr.onload = () => {
      let data: unknown;
      try {
        data = JSON.parse(xhr.responseText);
      } catch {
        reject(new FileApiError(
          xhr.statusText || 'Unknown error',
          xhr.status
        ));
        return;
      }

      if (xhr.status >= 200 && xhr.status < 300) {
        resolve(data as UploadResponse);
      } else {
        const errorData = data as ApiError | null;
        reject(new FileApiError(
          errorData?.message || 'Unknown error',
          xhr.status,
          errorData?.error
        ));
      }
    };

    xhr.onerror = () => {
      reject(new FileApiError('Network error', 0));
    };

    xhr.send(formData);
  });
}

/**
 * Which file space a call addresses: a conversation's own workspace or a
 * project's shared workspace.
 */
export type FileSource =
  | { kind: 'conversation'; id: string }
  | { kind: 'project'; id: string };

/**
 * A `FileSource`, or a bare conversation id. The bare-string form exists
 * only so the legacy conversation-space callers keep working unchanged; new
 * code should pass a `FileSource` (`conversationSource()` / `projectSource()`).
 */
export type FileTarget = FileSource | string;

export function conversationSource(conversationId: string): FileSource {
  return { kind: 'conversation', id: conversationId };
}

export function projectSource(projectId: string): FileSource {
  return { kind: 'project', id: projectId };
}

function toSource(target: FileTarget): FileSource {
  return typeof target === 'string' ? conversationSource(target) : target;
}

/** Stable per-space key (`conversation:<id>` / `project:<id>`) for UI state maps. */
export function fileSourceKey(source: FileSource): string {
  return `${source.kind}:${source.id}`;
}

/**
 * Root-relative base path of a space's file routes, e.g.
 * `/app/api/projects/p1/files`. Sub-routes append `/upload`, `/content`, ...
 */
export function fileRoutesBase(target: FileTarget): string {
  const source = toSource(target);
  const segment = source.kind === 'project' ? 'projects' : 'conversations';
  return `${API_BASE_URL}/${segment}/${encodeURIComponent(source.id)}/files`;
}

/**
 * Root-relative URL of the raw download route for one file -- what inline
 * previews (`<img src>`, PDF fetch) load. Cookie-authenticated.
 */
export function fileDownloadUrl(target: FileTarget, filePath: string): string {
  return `${fileRoutesBase(target)}/download?path=${encodeURIComponent(filePath)}`;
}

/** Absolute URL object for a file route (`suffix` like `/upload`, or '' for the list/delete root). */
function routeUrl(target: FileTarget, suffix: string): URL {
  return new URL(`${window.location.origin}${fileRoutesBase(target)}${suffix}`);
}

/** Pull the filename out of a Content-Disposition header, else `fallback`. */
function dispositionFilename(response: Response, fallback: string): string {
  const contentDisposition = response.headers.get('Content-Disposition');
  if (contentDisposition) {
    const match = contentDisposition.match(/filename="?([^"]+)"?/);
    if (match) return match[1];
  }
  return fallback;
}

/** GET/POST/DELETE a JSON file route, raising `FileApiError` on non-2xx. */
async function requestJson<T>(url: URL | string, init: RequestInit): Promise<T> {
  const response = await fetch(url.toString(), { credentials: 'include', ...init });
  if (!response.ok) {
    await handleErrorResponse(response);
  }
  return (await response.json()) as T;
}

/** Fetch a download route into a blob URL plus the server-chosen filename. */
async function fetchBlob(url: URL, fallbackName: string): Promise<{ url: string; filename: string }> {
  const response = await fetch(url.toString(), { method: 'GET', credentials: 'include' });
  if (!response.ok) {
    await handleErrorResponse(response);
  }
  const filename = dispositionFilename(response, fallbackName);
  const blob = await response.blob();
  return { url: URL.createObjectURL(blob), filename };
}

/**
 * List files in a workspace directory
 */
export async function listFiles(
  target: FileTarget,
  path: string
): Promise<ListFilesResponse> {
  const url = routeUrl(target, '');
  if (path) {
    url.searchParams.set('path', path);
  }
  return requestJson<ListFilesResponse>(url, {
    method: 'GET',
    headers: { 'Content-Type': 'application/json' },
  });
}

/**
 * Upload files to a workspace directory
 */
export async function uploadFiles(
  target: FileTarget,
  files: FileList | File[],
  path: string,
  onProgress?: (loaded: number, total: number) => void
): Promise<UploadResponse> {
  const url = routeUrl(target, '/upload');
  if (path) {
    url.searchParams.set('path', path);
  }

  const formData = new FormData();
  for (let i = 0; i < files.length; i++) {
    formData.append('files', files[i]);
  }

  return xhrUpload(url.toString(), formData, onProgress);
}

/**
 * Upload one or more clipboard images (PNG/JPEG only) for the composer.
 * Each file lands under ``workspace/pasted/<attachment_id>.<ext>``; the
 * server returns the per-file refs that the composer then carries on the
 * next ``send_message`` WS envelope. The endpoint is stricter than the
 * generic /files/upload route -- non-image MIME types are rejected.
 * Conversation-space only (there is no project twin).
 */
export async function uploadComposerAttachments(
  conversationId: string,
  files: File[],
): Promise<ComposerAttachmentUploadResponse> {
  const url = `${window.location.origin}${API_BASE_URL}/conversations/${conversationId}/composer-attachments`;

  const formData = new FormData();
  for (const file of files) {
    formData.append('files', file);
  }

  return requestJson<ComposerAttachmentUploadResponse>(url, {
    method: 'POST',
    body: formData,
  });
}

/**
 * Upload files with relative paths (for folder uploads).
 * Each file is sent with a corresponding relative path so the backend
 * can recreate the directory structure.
 */
export async function uploadFilesWithPaths(
  target: FileTarget,
  files: FileWithPath[],
  path: string,
  onProgress?: (loaded: number, total: number) => void
): Promise<UploadResponse> {
  const url = routeUrl(target, '/upload');
  if (path) {
    url.searchParams.set('path', path);
  }

  const formData = new FormData();
  for (const { file, relativePath } of files) {
    formData.append('files', file);
    formData.append('paths', relativePath);
  }

  return xhrUpload(url.toString(), formData, onProgress);
}

/**
 * Fetch text content of a file from the workspace for viewing
 */
export async function fetchFileContent(
  target: FileTarget,
  filePath: string
): Promise<FileContentResponse> {
  const url = routeUrl(target, '/content');
  url.searchParams.set('path', filePath);
  return requestJson<FileContentResponse>(url, { method: 'GET' });
}

/**
 * Hand a fetched blob to the browser as a file download (temporary anchor
 * click), then release the blob URL. Every workspace download ends here --
 * the hidden-data acknowledgement in contexts/DownloadWarningContext.tsx must
 * already have happened by the time a caller reaches this.
 */
export function saveBlobToDisk({ url, filename }: { url: string; filename: string }): void {
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
  // Deferred so the click has dispatched before the URL is revoked.
  setTimeout(() => URL.revokeObjectURL(url), 0);
}

/**
 * Which copy of a workspace file a download fetches: the file as stored, or
 * the metadata-stripped rewrite the server builds for raster images
 * (`/download-sanitized`, see chat/image_sanitizer.py -- 400
 * `unsanitizable_image` for anything that is not a PNG/JPEG/GIF/WebP).
 */
export type DownloadVariant = 'original' | 'sanitized';

/**
 * Download a file from the workspace
 * Returns a blob URL that can be used for download
 */
export async function downloadFile(
  target: FileTarget,
  filePath: string,
  variant: DownloadVariant = 'original',
): Promise<{ url: string; filename: string }> {
  const url = routeUrl(target, variant === 'sanitized' ? '/download-sanitized' : '/download');
  url.searchParams.set('path', filePath);
  return fetchBlob(url, filePath.split('/').pop() || 'download');
}

/**
 * Download a folder from the workspace as a zip archive
 * Returns a blob URL that can be used for download
 */
export async function downloadFolder(
  target: FileTarget,
  folderPath: string
): Promise<{ url: string; filename: string }> {
  const url = routeUrl(target, '/download-folder');
  url.searchParams.set('path', folderPath);
  return fetchBlob(url, folderPath.split('/').pop() + '.zip');
}

/**
 * Get info about a file or folder (name, type, file count)
 */
export async function getFileInfo(
  target: FileTarget,
  filePath: string
): Promise<FileInfoResponse> {
  const url = routeUrl(target, '/info');
  url.searchParams.set('path', filePath);
  return requestJson<FileInfoResponse>(url, { method: 'GET' });
}

/**
 * Save a workspace markdown file to Google Drive as a Google Doc.
 * Returns the created document's id, name, and url.
 */
export async function saveFileToDrive(
  target: FileTarget,
  filePath: string,
  title: string
): Promise<{ id: string; name: string; url: string }> {
  return requestJson<{ id: string; name: string; url: string }>(routeUrl(target, '/save-to-drive'), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ path: filePath, title }),
  });
}

/**
 * Create a new empty folder in the workspace under the given parent path.
 */
export async function createFolder(
  target: FileTarget,
  parentPath: string,
  name: string
): Promise<CreateFolderResponse> {
  return requestJson<CreateFolderResponse>(routeUrl(target, '/create-folder'), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ path: parentPath, name }),
  });
}

/**
 * Delete a file or folder from the workspace
 */
export async function deleteFile(
  target: FileTarget,
  filePath: string
): Promise<DeleteFileResponse> {
  const url = routeUrl(target, '');
  url.searchParams.set('path', filePath);
  return requestJson<DeleteFileResponse>(url, { method: 'DELETE' });
}

// ---------------------------------------------------------------------------
// Project twins: the same operations over a project's shared workspace.
// ---------------------------------------------------------------------------

export function listProjectFiles(projectId: string, path: string): Promise<ListFilesResponse> {
  return listFiles(projectSource(projectId), path);
}

export function uploadProjectFiles(
  projectId: string,
  files: FileList | File[],
  path: string,
  onProgress?: (loaded: number, total: number) => void,
): Promise<UploadResponse> {
  return uploadFiles(projectSource(projectId), files, path, onProgress);
}

export function uploadProjectFilesWithPaths(
  projectId: string,
  files: FileWithPath[],
  path: string,
  onProgress?: (loaded: number, total: number) => void,
): Promise<UploadResponse> {
  return uploadFilesWithPaths(projectSource(projectId), files, path, onProgress);
}

export function getProjectFileContent(projectId: string, filePath: string): Promise<FileContentResponse> {
  return fetchFileContent(projectSource(projectId), filePath);
}

export function projectFileDownloadUrl(projectId: string, filePath: string): string {
  return fileDownloadUrl(projectSource(projectId), filePath);
}

export function downloadProjectFile(
  projectId: string,
  filePath: string,
  variant: DownloadVariant = 'original',
): Promise<{ url: string; filename: string }> {
  return downloadFile(projectSource(projectId), filePath, variant);
}

export function downloadProjectFolder(projectId: string, folderPath: string): Promise<{ url: string; filename: string }> {
  return downloadFolder(projectSource(projectId), folderPath);
}

export function getProjectFileInfo(projectId: string, filePath: string): Promise<FileInfoResponse> {
  return getFileInfo(projectSource(projectId), filePath);
}

export function deleteProjectFile(projectId: string, filePath: string): Promise<DeleteFileResponse> {
  return deleteFile(projectSource(projectId), filePath);
}

export function createProjectFolder(projectId: string, parentPath: string, name: string): Promise<CreateFolderResponse> {
  return createFolder(projectSource(projectId), parentPath, name);
}

export function saveProjectFileToDrive(
  projectId: string,
  filePath: string,
  title: string,
): Promise<{ id: string; name: string; url: string }> {
  return saveFileToDrive(projectSource(projectId), filePath, title);
}

// ---------------------------------------------------------------------------
// Copy / move between a project conversation's workspace and its project's
// workspace (POST /conversations/{cid}/files/copy-to-project|copy-from-project).
// ---------------------------------------------------------------------------

export interface CopyEntryOptions {
  /** Source path in the source space. */
  path: string;
  /** Destination path in the other space; defaults to `path` server-side. */
  dest?: string;
  /** Replace an existing file / merge into an existing folder. */
  overwrite?: boolean;
  /** Remove the source after a successful copy. */
  move?: boolean;
  /** Copy dot-named entries inside a folder too. */
  includeHidden?: boolean;
}

/** Error code of the 409 the copy routes return for an existing destination. */
const DESTINATION_EXISTS = 'destination_exists';

/** True for the copy routes' 409 `destination_exists` (ask to overwrite, then retry with `overwrite: true`). */
export function isDestinationExistsError(err: unknown): err is FileApiError {
  return err instanceof FileApiError && err.statusCode === 409 && err.errorCode === DESTINATION_EXISTS;
}

async function copyBetweenSpaces(
  conversationId: string,
  direction: 'copy-to-project' | 'copy-from-project',
  options: CopyEntryOptions,
): Promise<CopyEntryResponse> {
  const body: Record<string, unknown> = { path: options.path };
  if (options.dest !== undefined) body.dest = options.dest;
  if (options.overwrite !== undefined) body.overwrite = options.overwrite;
  if (options.move !== undefined) body.move = options.move;
  if (options.includeHidden !== undefined) body.include_hidden = options.includeHidden;
  return requestJson<CopyEntryResponse>(
    routeUrl(conversationSource(conversationId), `/${direction}`),
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    },
  );
}

/**
 * Copy (or, with `move`, move) an entry from a project conversation's own
 * workspace into its project's workspace. Rejects with a `FileApiError`;
 * `isDestinationExistsError()` detects the 409 overwrite prompt case. A
 * move whose source removal failed resolves with `moved: false`.
 */
export function copyFileToProject(conversationId: string, options: CopyEntryOptions): Promise<CopyEntryResponse> {
  return copyBetweenSpaces(conversationId, 'copy-to-project', options);
}

/** The reverse of `copyFileToProject`: project workspace -> conversation workspace. */
export function copyFileFromProject(conversationId: string, options: CopyEntryOptions): Promise<CopyEntryResponse> {
  return copyBetweenSpaces(conversationId, 'copy-from-project', options);
}
