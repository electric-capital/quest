/**
 * React hook for file browser functionality over one file space (a
 * conversation's workspace or a project's shared workspace, see
 * `FileSource` in api/fileApi.ts).
 */

import { useState, useCallback, useEffect, useMemo, useRef } from 'react';
import type { FileEntry, ListFilesResponse, UploadResponse } from '../api/types';
import { listFiles, uploadFiles as apiUploadFiles, uploadFilesWithPaths as apiUploadFilesWithPaths, downloadFile as apiDownloadFile, downloadFolder as apiDownloadFolder, deleteFile as apiDeleteFile, createFolder as apiCreateFolder, saveBlobToDisk, FileApiError, fileSourceKey, type FileSource } from '../api/fileApi';
import type { FileWithPath } from '../utils/directoryTraversal';
import { useFileBrowserState, INITIAL_FILE_BROWSER_STATE } from '../contexts/FileBrowserStateContext';
import { useDownloadWarning } from '../contexts/DownloadWarningContext';

/**
 * Build a user-facing summary when an upload response contains partial errors.
 * Returns null if there are no errors to report.
 */
function buildUploadErrorMessage(response: UploadResponse): string | null {
  if (!response.errors || response.errors.length === 0) {
    return null;
  }

  const successCount = response.uploadedFiles?.length ?? 0;
  const failCount = response.errors.length;

  const lines: string[] = [];

  if (successCount > 0) {
    lines.push(`${successCount} file${successCount !== 1 ? 's' : ''} uploaded. ${failCount} failed:`);
  } else {
    lines.push(`${failCount} file${failCount !== 1 ? 's' : ''} failed to upload:`);
  }

  for (const err of response.errors) {
    lines.push(`${err.filename}: ${err.message}`);
  }

  return lines.join('\n');
}

interface UseFileBrowserResult {
  // State
  currentPath: string;
  files: FileEntry[];
  loading: boolean;
  error: string | null;
  uploadProgress: boolean;
  uploadPercent: number | null;
  canGoUp: boolean;
  canGoBack: boolean;
  canGoForward: boolean;
  zippingFolder: string | null;
  /** Dot-prefixed entries revealed; persisted per file space. */
  showHidden: boolean;

  // Methods
  fetchFiles: () => Promise<void>;
  navigateToFolder: (folderName: string) => void;
  goBack: () => void;
  goForward: () => void;
  goUp: () => void;
  uploadFiles: (files: FileList | File[]) => Promise<UploadResponse>;
  uploadFilesWithPaths: (files: FileWithPath[]) => Promise<UploadResponse>;
  downloadFile: (filePath: string) => Promise<void>;
  downloadFolder: (folderPath: string) => Promise<void>;
  deleteItem: (filePath: string) => Promise<void>;
  createFolder: (name: string) => Promise<void>;
  refresh: () => Promise<void>;
  silentRefresh: () => Promise<void>;
  setShowHidden: (showHidden: boolean) => void;
}

/**
 * Hook for managing file browser state and operations. `source` null = no
 * file space (empty panel). Navigation state lives in FileBrowserStateContext
 * under `fileSourceKey(source)`, so two cards over different spaces never
 * share a path, history or dotfile toggle.
 */
export function useFileBrowser(source: FileSource | null): UseFileBrowserResult {
  const { getFileBrowserState, setFileBrowserState } = useFileBrowserState();
  const { confirmDownload } = useDownloadWarning();

  // Re-derive the source from its primitives so callers may pass a fresh
  // object literal every render without re-running every effect.
  const sourceKind = source?.kind ?? null;
  const sourceId = source?.id ?? null;
  const activeSource = useMemo<FileSource | null>(
    () => (sourceKind && sourceId ? { kind: sourceKind, id: sourceId } : null),
    [sourceKind, sourceId],
  );
  // State key of the active file space (`conversation:<id>` /
  // `project:<id>`); the guards compare keys, API calls use `activeSource`.
  const sourceKey = activeSource ? fileSourceKey(activeSource) : null;

  // Get state from context (file-space-specific)
  const browserState = sourceKey ? getFileBrowserState(sourceKey) : INITIAL_FILE_BROWSER_STATE;
  const currentPath = browserState.path;
  const navigationHistory = browserState.history;
  const historyIndex = browserState.historyIndex;

  // Local UI state (not persisted per file space)
  const [files, setFiles] = useState<FileEntry[]>([]);
  const [loading, setLoading] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const [uploadProgress, setUploadProgress] = useState<boolean>(false);
  const [uploadPercent, setUploadPercent] = useState<number | null>(null);
  const [canGoUp, setCanGoUp] = useState<boolean>(false);
  const [zippingFolder, setZippingFolder] = useState<string | null>(null);

  // Refs to break circular dependencies and prevent rapid refresh.
  // inFlightRef tracks the file space + path of the request currently in
  // flight (or null when idle). It is scoped per-space/per-path so an
  // in-flight fetch for one space never swallows the first fetch for a
  // different space (which would leave the panel showing stale files).
  const inFlightRef = useRef<{ sourceKey: string; path: string } | null>(null);
  const browserStateRef = useRef(browserState);
  browserStateRef.current = browserState;
  // Mirror of the latest sourceKey for use inside async fetch handlers
  // (lets the out-of-order guard compare against the *current* file space
  // without depending on stale closure values).
  const sourceKeyRef = useRef<string | null>(sourceKey);
  sourceKeyRef.current = sourceKey;
  // Tracks the file space the consolidated fetch effect last ran for, so it
  // can tell a space switch (e.g. a conversation switch: silent,
  // stale-while-revalidate) apart from in-space path navigation
  // (loading-visible).
  const prevSourceKeyRef = useRef<string | null>(sourceKey);

  // Helper to update browser state in context - uses ref to avoid dependency on browserState
  const updateBrowserState = useCallback((updates: Partial<typeof browserState>) => {
    if (!sourceKey) return;
    setFileBrowserState(sourceKey, { ...browserStateRef.current, ...updates });
  }, [sourceKey, setFileBrowserState]);

  // Fetch files for current path - stable callback that doesn't depend on updateBrowserState
  // silent=true skips loading state updates (used for background refreshes)
  const fetchFilesInternal = useCallback(async (silent: boolean = false) => {
    if (!sourceKey || !activeSource) {
      setFiles([]);
      setError(null);
      return;
    }

    // Guard against duplicate concurrent fetches for the *same* file space +
    // path. A request for a different space/path is always allowed
    // through so a switch is never swallowed by a stale in-flight fetch.
    const inFlight = inFlightRef.current;
    if (inFlight && inFlight.sourceKey === sourceKey && inFlight.path === currentPath) {
      return;
    }
    // Capture the id/path this request belongs to so the response handler can
    // ignore out-of-order results (e.g. a slow A response landing after the
    // user switched to B, or A->B->A rapid switching).
    const requestSourceKey = sourceKey;
    const requestSource = activeSource;
    const requestPath = currentPath;
    inFlightRef.current = { sourceKey: requestSourceKey, path: requestPath };

    if (!silent) {
      setLoading(true);
    }
    setError(null);

    try {
      const response: ListFilesResponse = await listFiles(requestSource, requestPath);
      // Drop the response if the active file space/path has moved on while
      // this request was in flight - otherwise we'd clobber the current list.
      if (browserStateRef.current.path !== requestPath || sourceKeyRef.current !== requestSourceKey) {
        return;
      }
      setFiles(response.files);
      setCanGoUp(response.canGoUp);

      // Update current path from server response (normalized) - use ref to get latest state
      if (response.currentPath !== requestPath) {
        setFileBrowserState(requestSourceKey, { ...browserStateRef.current, path: response.currentPath });
      }
    } catch (err) {
      // Only surface errors for the still-current file space/path.
      if (browserStateRef.current.path !== requestPath || sourceKeyRef.current !== requestSourceKey) {
        return;
      }
      if (err instanceof FileApiError) {
        setError(err.message);
      } else {
        setError('Failed to load files');
      }
      setFiles([]);
    } finally {
      if (!silent) {
        setLoading(false);
      }
      // Only clear the in-flight marker if it still refers to this request;
      // a newer fetch for a different file space/path may have replaced it.
      const current = inFlightRef.current;
      if (current && current.sourceKey === requestSourceKey && current.path === requestPath) {
        inFlightRef.current = null;
      }
    }
  }, [sourceKey, activeSource, currentPath, setFileBrowserState]);

  // Public fetchFiles - shows loading state
  const fetchFiles = useCallback(async () => {
    await fetchFilesInternal(false);
  }, [fetchFilesInternal]);

  // Single fetch effect for both file-space switches and in-space
  // path navigation. Consolidating these avoids the previous A/B effect race
  // where two effects fired in the same commit and the shared in-flight guard
  // swallowed the second fetch. A ref tracks the previous file space so we
  // can tell the two cases apart: a space switch uses a silent fetch
  // (but resets stale state first so the old space's files can't show),
  // while path navigation shows the loading state.
  useEffect(() => {
    const sourceChanged = prevSourceKeyRef.current !== sourceKey;
    prevSourceKeyRef.current = sourceKey;

    if (!sourceKey) {
      // No file space - clear the panel.
      setFiles([]);
      setError(null);
      return;
    }

    if (sourceChanged) {
      // Reset stale per-space UI state so the new file space can
      // never momentarily display the previous one's files, then refetch
      // silently (stale-while-revalidate with no spinner flash).
      setFiles([]);
      setCanGoUp(false);
      setError(null);
      fetchFilesInternal(true);
    } else {
      // In-space path navigation - show the loading state.
      fetchFilesInternal(false);
    }
  }, [sourceKey, currentPath, fetchFilesInternal]);

  // Navigate to a folder
  const navigateToFolder = useCallback((folderName: string) => {
    const newPath = currentPath === '/'
      ? `/${folderName}`
      : `${currentPath}/${folderName}`;

    // Add to history
    const newHistory = navigationHistory.slice(0, historyIndex + 1);
    newHistory.push(newPath);
    updateBrowserState({
      path: newPath,
      history: newHistory,
      historyIndex: newHistory.length - 1,
    });
  }, [currentPath, navigationHistory, historyIndex, updateBrowserState]);

  // Go back in history
  const goBack = useCallback(() => {
    if (historyIndex > 0) {
      const newIndex = historyIndex - 1;
      updateBrowserState({
        path: navigationHistory[newIndex],
        historyIndex: newIndex,
      });
    }
  }, [historyIndex, navigationHistory, updateBrowserState]);

  // Go forward in history
  const goForward = useCallback(() => {
    if (historyIndex < navigationHistory.length - 1) {
      const newIndex = historyIndex + 1;
      updateBrowserState({
        path: navigationHistory[newIndex],
        historyIndex: newIndex,
      });
    }
  }, [historyIndex, navigationHistory, updateBrowserState]);

  // Go up one directory
  const goUp = useCallback(() => {
    if (!canGoUp || currentPath === '/') return;

    const parts = currentPath.split('/').filter(Boolean);
    parts.pop();
    const newPath = parts.length === 0 ? '/' : `/${parts.join('/')}`;

    // Add to history
    const newHistory = navigationHistory.slice(0, historyIndex + 1);
    newHistory.push(newPath);
    updateBrowserState({
      path: newPath,
      history: newHistory,
      historyIndex: newHistory.length - 1,
    });
  }, [canGoUp, currentPath, navigationHistory, historyIndex, updateBrowserState]);

  // Upload files
  const uploadFiles = useCallback(async (filesToUpload: FileList | File[]): Promise<UploadResponse> => {
    if (!activeSource) {
      throw new Error('No file space selected');
    }

    setUploadProgress(true);
    setUploadPercent(0);
    setError(null);

    try {
      const response = await apiUploadFiles(activeSource, filesToUpload, currentPath, (loaded, total) => {
        setUploadPercent(Math.round((loaded / total) * 100));
      });

      // Refresh file list after upload
      await fetchFiles();

      // Surface partial errors (some files succeeded, some failed)
      const errorMsg = buildUploadErrorMessage(response);
      if (errorMsg) {
        setError(errorMsg);
      }

      return response;
    } catch (err) {
      if (err instanceof FileApiError) {
        setError(err.message);
      } else {
        setError('Failed to upload files');
      }
      throw err;
    } finally {
      setUploadProgress(false);
      setUploadPercent(null);
    }
  }, [activeSource, currentPath, fetchFiles]);

  // Upload files with relative paths (for folder uploads)
  const uploadFilesWithPaths = useCallback(async (filesToUpload: FileWithPath[]): Promise<UploadResponse> => {
    if (!activeSource) {
      throw new Error('No file space selected');
    }

    setUploadProgress(true);
    setUploadPercent(0);
    setError(null);

    try {
      const response = await apiUploadFilesWithPaths(activeSource, filesToUpload, currentPath, (loaded, total) => {
        setUploadPercent(Math.round((loaded / total) * 100));
      });

      // Refresh file list after upload
      await fetchFiles();

      // Surface partial errors (some files succeeded, some failed)
      const errorMsg = buildUploadErrorMessage(response);
      if (errorMsg) {
        setError(errorMsg);
      }

      return response;
    } catch (err) {
      if (err instanceof FileApiError) {
        setError(err.message);
      } else {
        setError('Failed to upload files');
      }
      throw err;
    } finally {
      setUploadProgress(false);
      setUploadPercent(null);
    }
  }, [activeSource, currentPath, fetchFiles]);

  // Download file (after the hidden-data acknowledgement when the type needs
  // one; the user may pick the server's sanitized copy of a raster image)
  const downloadFile = useCallback(async (filePath: string): Promise<void> => {
    if (!activeSource) {
      throw new Error('No file space selected');
    }

    const name = filePath.split('/').filter(Boolean).pop() || filePath;
    const decision = await confirmDownload({ name, kind: 'file' });
    if (decision === 'cancel') return;

    try {
      saveBlobToDisk(await apiDownloadFile(activeSource, filePath, decision));
    } catch (err) {
      if (err instanceof FileApiError) {
        setError(err.message);
      } else {
        setError('Failed to download file');
      }
      throw err;
    }
  }, [activeSource, confirmDownload]);

  // Download folder as zip (always behind the hidden-data acknowledgement:
  // an archive can hold anything)
  const downloadFolder = useCallback(async (folderPath: string): Promise<void> => {
    if (!activeSource) {
      throw new Error('No file space selected');
    }

    // Extract folder name from path for the notification
    const folderName = folderPath.split('/').filter(Boolean).pop() || 'folder';
    if ((await confirmDownload({ name: folderName, kind: 'folder' })) === 'cancel') return;
    setZippingFolder(folderName);

    try {
      saveBlobToDisk(await apiDownloadFolder(activeSource, folderPath));
    } catch (err) {
      if (err instanceof FileApiError) {
        setError(err.message);
      } else {
        setError('Failed to download folder');
      }
      throw err;
    } finally {
      setZippingFolder(null);
    }
  }, [activeSource, confirmDownload]);

  // Delete a file or folder
  const deleteItem = useCallback(async (filePath: string): Promise<void> => {
    if (!activeSource) {
      throw new Error('No file space selected');
    }

    try {
      await apiDeleteFile(activeSource, filePath);

      // Refresh file list after deletion
      await fetchFiles();
    } catch (err) {
      if (err instanceof FileApiError) {
        setError(err.message);
      } else {
        setError('Failed to delete item');
      }
      throw err;
    }
  }, [activeSource, fetchFiles]);

  // Create a new empty folder in the current directory
  const createFolder = useCallback(async (name: string): Promise<void> => {
    if (!activeSource) {
      throw new Error('No file space selected');
    }

    setError(null);

    try {
      await apiCreateFolder(activeSource, currentPath, name);
      // Refresh the listing so the new folder appears
      await fetchFiles();
    } catch (err) {
      if (err instanceof FileApiError) {
        setError(err.message);
      } else {
        setError('Failed to create folder');
      }
      throw err;
    }
  }, [activeSource, currentPath, fetchFiles]);

  // Dotfile toggle, persisted with the space's navigation state.
  const setShowHidden = useCallback((showHidden: boolean) => {
    updateBrowserState({ showHidden });
  }, [updateBrowserState]);

  // Refresh current directory (shows loading state)
  const refresh = useCallback(async () => {
    await fetchFilesInternal(false);
  }, [fetchFilesInternal]);

  // Silent refresh (no loading state - for background auto-refresh)
  const silentRefresh = useCallback(async () => {
    await fetchFilesInternal(true);
  }, [fetchFilesInternal]);

  return {
    currentPath,
    files,
    loading,
    error,
    uploadProgress,
    uploadPercent,
    canGoUp,
    canGoBack: historyIndex > 0,
    canGoForward: historyIndex < navigationHistory.length - 1,
    zippingFolder,
    showHidden: browserState.showHidden,
    fetchFiles,
    navigateToFolder,
    goBack,
    goForward,
    goUp,
    uploadFiles,
    uploadFilesWithPaths,
    downloadFile,
    downloadFolder,
    deleteItem,
    createFolder,
    refresh,
    silentRefresh,
    setShowHidden,
  };
}
