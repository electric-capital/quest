/**
 * API client for file browser functionality
 */

import { API_BASE_URL } from './config';
import type {
  ListFilesResponse,
  UploadResponse,
  FileContentResponse,
  FileInfoResponse,
  DeleteFileResponse,
  CreateFolderResponse,
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
 * List files in a workspace directory
 */
export async function listFiles(
  conversationId: string,
  path: string
): Promise<ListFilesResponse> {
  const url = new URL(`${window.location.origin}${API_BASE_URL}/conversations/${conversationId}/files`);
  if (path) {
    url.searchParams.set('path', path);
  }

  const response = await fetch(url.toString(), {
    method: 'GET',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
  });

  if (!response.ok) {
    await handleErrorResponse(response);
  }

  return await response.json();
}

/**
 * Upload files to a workspace directory
 */
export async function uploadFiles(
  conversationId: string,
  files: FileList | File[],
  path: string,
  onProgress?: (loaded: number, total: number) => void
): Promise<UploadResponse> {
  const url = new URL(`${window.location.origin}${API_BASE_URL}/conversations/${conversationId}/files/upload`);
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

  const response = await fetch(url, {
    method: 'POST',
    body: formData,
    credentials: 'include',
  });

  if (!response.ok) {
    await handleErrorResponse(response);
  }

  return (await response.json()) as ComposerAttachmentUploadResponse;
}

/**
 * Upload files with relative paths (for folder uploads).
 * Each file is sent with a corresponding relative path so the backend
 * can recreate the directory structure.
 */
export async function uploadFilesWithPaths(
  conversationId: string,
  files: FileWithPath[],
  path: string,
  onProgress?: (loaded: number, total: number) => void
): Promise<UploadResponse> {
  const url = new URL(
    `${window.location.origin}${API_BASE_URL}/conversations/${conversationId}/files/upload`
  );
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
  conversationId: string,
  filePath: string
): Promise<FileContentResponse> {
  const url = new URL(`${window.location.origin}${API_BASE_URL}/conversations/${conversationId}/files/content`);
  url.searchParams.set('path', filePath);

  const response = await fetch(url.toString(), {
    method: 'GET',
    credentials: 'include',
  });

  if (!response.ok) {
    await handleErrorResponse(response);
  }

  return await response.json();
}

/**
 * Download a file from the workspace
 * Returns a blob URL that can be used for download
 */
export async function downloadFile(
  conversationId: string,
  filePath: string
): Promise<{ url: string; filename: string }> {
  const url = new URL(`${window.location.origin}${API_BASE_URL}/conversations/${conversationId}/files/download`);
  url.searchParams.set('path', filePath);

  const response = await fetch(url.toString(), {
    method: 'GET',
    credentials: 'include',
  });

  if (!response.ok) {
    await handleErrorResponse(response);
  }

  // Get filename from Content-Disposition header or use path
  const contentDisposition = response.headers.get('Content-Disposition');
  let filename = filePath.split('/').pop() || 'download';
  if (contentDisposition) {
    const match = contentDisposition.match(/filename="?([^"]+)"?/);
    if (match) {
      filename = match[1];
    }
  }

  // Create blob URL
  const blob = await response.blob();
  const blobUrl = URL.createObjectURL(blob);

  return { url: blobUrl, filename };
}

/**
 * Download a folder from the workspace as a zip archive
 * Returns a blob URL that can be used for download
 */
export async function downloadFolder(
  conversationId: string,
  folderPath: string
): Promise<{ url: string; filename: string }> {
  const url = new URL(`${window.location.origin}${API_BASE_URL}/conversations/${conversationId}/files/download-folder`);
  url.searchParams.set('path', folderPath);

  const response = await fetch(url.toString(), {
    method: 'GET',
    credentials: 'include',
  });

  if (!response.ok) {
    await handleErrorResponse(response);
  }

  // Get filename from Content-Disposition header or use path
  const contentDisposition = response.headers.get('Content-Disposition');
  let filename = folderPath.split('/').pop() + '.zip';
  if (contentDisposition) {
    const match = contentDisposition.match(/filename="?([^"]+)"?/);
    if (match) {
      filename = match[1];
    }
  }

  // Create blob URL
  const blob = await response.blob();
  const blobUrl = URL.createObjectURL(blob);

  return { url: blobUrl, filename };
}

/**
 * Get info about a file or folder (name, type, file count)
 */
export async function getFileInfo(
  conversationId: string,
  filePath: string
): Promise<FileInfoResponse> {
  const url = new URL(`${window.location.origin}${API_BASE_URL}/conversations/${conversationId}/files/info`);
  url.searchParams.set('path', filePath);

  const response = await fetch(url.toString(), {
    method: 'GET',
    credentials: 'include',
  });

  if (!response.ok) {
    await handleErrorResponse(response);
  }

  return await response.json();
}

/**
 * Save a workspace markdown file to Google Drive as a Google Doc.
 * Returns the created document's id, name, and url.
 */
export async function saveFileToDrive(
  conversationId: string,
  filePath: string,
  title: string
): Promise<{ id: string; name: string; url: string }> {
  const url = `${window.location.origin}${API_BASE_URL}/conversations/${conversationId}/files/save-to-drive`;

  const response = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify({ path: filePath, title }),
  });

  if (!response.ok) {
    await handleErrorResponse(response);
  }

  return await response.json();
}

/**
 * Create a new empty folder in the workspace under the given parent path.
 */
export async function createFolder(
  conversationId: string,
  parentPath: string,
  name: string
): Promise<CreateFolderResponse> {
  const url = `${window.location.origin}${API_BASE_URL}/conversations/${conversationId}/files/create-folder`;

  const response = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify({ path: parentPath, name }),
  });

  if (!response.ok) {
    await handleErrorResponse(response);
  }

  return await response.json();
}

/**
 * Delete a file or folder from the workspace
 */
export async function deleteFile(
  conversationId: string,
  filePath: string
): Promise<DeleteFileResponse> {
  const url = new URL(`${window.location.origin}${API_BASE_URL}/conversations/${conversationId}/files`);
  url.searchParams.set('path', filePath);

  const response = await fetch(url.toString(), {
    method: 'DELETE',
    credentials: 'include',
  });

  if (!response.ok) {
    await handleErrorResponse(response);
  }

  return await response.json();
}
