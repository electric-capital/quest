/**
 * File browser panel component for workspace file management. One card per
 * file space (`source`): a conversation's own workspace ("Chat Files") or a
 * project's shared workspace ("Project Files"). Every operation -- listing,
 * upload, new folder, delete, downloads, the viewer and its Save to Drive --
 * goes through that source's routes, and the card refreshes only on
 * `file_list_changed` events for its own space.
 */

import { useRef, useState, useMemo, useCallback, useEffect, type DragEvent } from 'react';
import { useFileBrowser } from '../hooks/useFileBrowser';
import { getFileInfo, type FileSource } from '../api/fileApi';
import { persistentWebSocket } from '../services/PersistentWebSocket';
import { extractFilesFromDataTransfer } from '../utils/directoryTraversal';
import { getFileIconInfo } from '../utils/fileIcons';
import { FileViewerModal } from './FileViewerModal';
import { NewFolderModal } from './NewFolderModal';
import { Folder, Upload, FolderPlus, Eye, EyeOff } from 'lucide-react';
import type { FileEntry } from '../api/types';
import './FileBrowser.css';

// Coalesce bursts of file_list_changed events (e.g. ten write_workspace_file
// calls in one turn) into a single silent refresh.
const FILE_LIST_REFRESH_DEBOUNCE_MS = 200;

const TEXT_EXTENSIONS = new Set(['.md', '.py', '.txt', '.json']);
const IMAGE_EXTENSIONS = new Set(['.png', '.jpg', '.jpeg', '.gif', '.svg', '.webp', '.bmp', '.ico', '.avif']);
const PDF_EXTENSIONS = new Set(['.pdf']);
const CSV_EXTENSIONS = new Set(['.csv']);

function isViewableFile(name: string): boolean {
  const dotIndex = name.lastIndexOf('.');
  const ext = dotIndex >= 0 ? name.substring(dotIndex).toLowerCase() : '';
  return TEXT_EXTENSIONS.has(ext) || IMAGE_EXTENSIONS.has(ext) || PDF_EXTENSIONS.has(ext) || CSV_EXTENSIONS.has(ext);
}

// The type sniffers below are exported for reuse by other FileViewerModal
// hosts (e.g. SubagentReturnFilesPreview) so the flag logic stays in one place.
export function isImageFile(name: string): boolean {
  const dotIndex = name.lastIndexOf('.');
  const ext = dotIndex >= 0 ? name.substring(dotIndex).toLowerCase() : '';
  return IMAGE_EXTENSIONS.has(ext);
}

export function isJsonFile(name: string): boolean {
  return name.toLowerCase().endsWith('.json');
}

export function isPdfFile(name: string): boolean {
  return name.toLowerCase().endsWith('.pdf');
}

export function isCsvFile(name: string): boolean {
  return name.toLowerCase().endsWith('.csv');
}

/** The row a per-row action was opened on. */
export interface FileRowContext {
  entry: FileEntry;
  /** Root-relative path of the entry in its space, e.g. `/reports/q3.csv`. */
  path: string;
  /** The space the card browses. */
  source: FileSource;
}

/**
 * An extra item in a row's actions menu, supplied by the host through
 * `rowActions` (e.g. Copy / Move between Chat Files and Project Files).
 * Rendered after the built-in Download item and before Delete; the menu
 * closes before `onSelect` runs.
 */
export interface FileRowAction {
  /** Stable React key, unique within one row's menu. */
  key: string;
  label: string;
  onSelect: () => void;
  disabled?: boolean;
  /** Danger styling (like Delete). */
  danger?: boolean;
}

interface FileBrowserProps {
  /**
   * The file space browsed: a conversation's own workspace (card titled
   * "Chat Files") or a project's shared workspace ("Project Files"). Null
   * renders the no-conversation empty state.
   */
  source: FileSource | null;
  /**
   * Extra per-row actions for the row's menu. Called on render for each row
   * whose menu is open; return [] for none.
   */
  rowActions?: (row: FileRowContext) => FileRowAction[];
}

/** Card title for a file space. */
export function fileBrowserTitle(source: FileSource | null): string {
  if (!source) return 'Files';
  return source.kind === 'project' ? 'Project Files' : 'Chat Files';
}

/**
 * Whether a ``file_list_changed`` event concerns the space a card shows:
 * conversation routes/tools publish scope "conversation" with the
 * conversation id, project routes publish scope "project" with the project
 * id (and a null conversation id); copy routes and sandbox runs publish one
 * event per scope.
 */
export function fileListEventMatchesSource(
  event: Record<string, unknown>,
  source: FileSource,
): boolean {
  if (source.kind === 'conversation') {
    return event.scope === 'conversation' && event.conversation_id === source.id;
  }
  return event.scope === 'project' && event.project_id === source.id;
}

function formatFileSize(bytes: number | null): string {
  if (bytes === null) return '';
  if (bytes === 0) return '0 B';
  const k = 1024;
  const sizes = ['B', 'KB', 'MB', 'GB'];
  const i = Math.floor(Math.log(bytes) / Math.log(k));
  return parseFloat((bytes / Math.pow(k, i)).toFixed(1)) + ' ' + sizes[i];
}

function formatDate(isoString: string): string {
  const date = new Date(isoString);
  return date.toLocaleDateString(undefined, {
    month: 'short',
    day: 'numeric',
    year: date.getFullYear() !== new Date().getFullYear() ? 'numeric' : undefined,
  });
}

export function FileBrowser({ source, rowActions }: FileBrowserProps) {
  // Primitives for effect deps: hosts may pass a fresh object every render.
  const sourceKind = source?.kind ?? null;
  const sourceId = source?.id ?? null;
  const {
    currentPath,
    files,
    loading,
    error,
    uploadProgress,
    uploadPercent,
    canGoUp,
    canGoBack,
    canGoForward,
    zippingFolder,
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
    showHidden,
    setShowHidden,
  } = useFileBrowser(source);

  const fileInputRef = useRef<HTMLInputElement>(null);
  const [isDragOver, setIsDragOver] = useState(false);
  const [viewerFile, setViewerFile] = useState<{ path: string; name: string; isImage: boolean; isJson: boolean; isPdf: boolean; isCsv: boolean } | null>(null);
  const [openMenuItemName, setOpenMenuItemName] = useState<string | null>(null);
  const [newFolderModalOpen, setNewFolderModalOpen] = useState(false);
  // Dotfiles (e.g. machine-written `.responses/`, scratch `.temp/`) are hidden
  // by default to keep the listing clean; the toolbar toggle reveals them.
  // Kept per file space in FileBrowserStateContext (via useFileBrowser), so
  // the Chat Files and Project Files cards toggle independently.

  // Only show loading state for buttons during initial load (no files yet)
  const isInitialLoading = loading && files.length === 0;

  // Root-relative path of a row in the current directory.
  const entryPath = (item: FileEntry) =>
    currentPath === '/' ? `/${item.name}` : `${currentPath}/${item.name}`;

  // Files actually rendered: drop dot-prefixed entries unless the user has
  // toggled "show hidden" on. Filtering is purely client-side -- the list
  // endpoint returns every entry so the toggle can reveal them without a
  // refetch.
  const visibleFiles = useMemo(
    () => (showHidden ? files : files.filter((item) => !item.name.startsWith('.'))),
    [files, showHidden]
  );
  const hiddenCount = files.length - visibleFiles.length;

  // Auto-refresh when the BE publishes a ``file_list_changed`` per-user
  // global. Filters by scope/id so only events for this card's own space
  // trigger a refetch (see fileListEventMatchesSource). A short debounce
  // coalesces bursts (e.g. several write_file calls in one turn) into a
  // single silent fetch.
  useEffect(() => {
    if (!sourceKind || !sourceId) return;
    const watched: FileSource = { kind: sourceKind, id: sourceId };
    let timer: ReturnType<typeof setTimeout> | null = null;
    const unsubscribe = persistentWebSocket.onGlobalEvent((event) => {
      if (event.type !== 'file_list_changed') return;
      if (!fileListEventMatchesSource(event, watched)) return;

      if (timer !== null) clearTimeout(timer);
      timer = setTimeout(() => {
        timer = null;
        silentRefresh();
      }, FILE_LIST_REFRESH_DEBOUNCE_MS);
    });
    return () => {
      unsubscribe();
      if (timer !== null) clearTimeout(timer);
    };
  }, [sourceKind, sourceId, silentRefresh]);

  // Close meatball menu on click outside
  useEffect(() => {
    if (!openMenuItemName) return;
    const handleClickOutside = () => setOpenMenuItemName(null);
    document.addEventListener('click', handleClickOutside);
    return () => document.removeEventListener('click', handleClickOutside);
  }, [openMenuItemName]);

  // Handle file input change
  const handleFileInputChange = useCallback(async (e: React.ChangeEvent<HTMLInputElement>) => {
    const files = e.target.files;
    if (files && files.length > 0) {
      try {
        await uploadFiles(files);
      } catch {
        // Error is handled in the hook
      }
      // Reset input
      if (fileInputRef.current) {
        fileInputRef.current.value = '';
      }
    }
  }, [uploadFiles]);

  // Handle upload button click
  const handleUploadClick = useCallback(() => {
    fileInputRef.current?.click();
  }, []);

  // Handle "New Folder" button click - opens the modal
  const handleNewFolderClick = useCallback(() => {
    setNewFolderModalOpen(true);
  }, []);

  // Forward modal submit to the hook so the listing refreshes on success.
  // Errors propagate so the modal can keep itself open and show the message.
  const handleNewFolderSubmit = useCallback(async (name: string) => {
    await createFolder(name);
  }, [createFolder]);

  // Handle drag events
  const handleDragOver = useCallback((e: DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    e.stopPropagation();
    setIsDragOver(true);
  }, []);

  const handleDragLeave = useCallback((e: DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    e.stopPropagation();
    setIsDragOver(false);
  }, []);

  const handleDrop = useCallback(async (e: DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    e.stopPropagation();
    setIsDragOver(false);

    const filesWithPaths = await extractFilesFromDataTransfer(e.dataTransfer);
    if (filesWithPaths.length > 0) {
      try {
        await uploadFilesWithPaths(filesWithPaths);
      } catch {
        // Error is handled in the hook
      }
    }
  }, [uploadFilesWithPaths]);

  // Handle file/folder click
  const handleItemClick = useCallback((item: FileEntry) => {
    if (item.type === 'folder') {
      navigateToFolder(item.name);
    } else if (isViewableFile(item.name)) {
      const filePath = currentPath === '/' ? `/${item.name}` : `${currentPath}/${item.name}`;
      setViewerFile({ path: filePath, name: item.name, isImage: isImageFile(item.name), isJson: isJsonFile(item.name), isPdf: isPdfFile(item.name), isCsv: isCsvFile(item.name) });
    }
  }, [navigateToFolder, currentPath]);

  // Handle download click
  const handleDownloadClick = useCallback(async (e: React.MouseEvent, item: FileEntry) => {
    e.stopPropagation();
    const filePath = currentPath === '/' ? `/${item.name}` : `${currentPath}/${item.name}`;
    try {
      await downloadFile(filePath);
    } catch {
      // Error is handled in the hook
    }
  }, [currentPath, downloadFile]);

  // Handle folder download click (download as zip)
  const handleFolderDownloadClick = useCallback(async (e: React.MouseEvent, item: FileEntry) => {
    e.stopPropagation();
    setOpenMenuItemName(null);
    const filePath = currentPath === '/' ? `/${item.name}` : `${currentPath}/${item.name}`;
    try {
      await downloadFolder(filePath);
    } catch {
      // Error is handled in the hook
    }
  }, [currentPath, downloadFolder]);

  // Handle delete click
  const handleDeleteClick = useCallback(async (e: React.MouseEvent, item: FileEntry) => {
    e.stopPropagation();
    setOpenMenuItemName(null);

    const filePath = currentPath === '/' ? `/${item.name}` : `${currentPath}/${item.name}`;

    if (item.type === 'folder') {
      // Get file count for folder before confirming
      try {
        if (!source) return;
        const info = await getFileInfo(source, filePath);
        const confirmed = window.confirm(
          `Delete folder "${item.name}"? This will delete ${info.fileCount} file${info.fileCount !== 1 ? 's' : ''}.`
        );
        if (!confirmed) return;
      } catch {
        // If we can't get the count, still allow deletion with a generic prompt
        const confirmed = window.confirm(`Delete folder "${item.name}" and all its contents?`);
        if (!confirmed) return;
      }
    } else {
      const confirmed = window.confirm(`Delete "${item.name}"?`);
      if (!confirmed) return;
    }

    try {
      await deleteItem(filePath);
    } catch {
      // Error is handled in the hook
    }
  }, [currentPath, source, deleteItem]);

  // No file space: nothing to browse until a conversation is selected.
  if (!source) {
    return (
      <div className="file-browser">
        <div className="file-browser-header">
          <h3>{fileBrowserTitle(null)}</h3>
        </div>
        <div className="file-browser-empty">
          <p>Select a conversation to browse files</p>
        </div>
      </div>
    );
  }

  return (
    <div
      className={`file-browser ${isDragOver ? 'drag-over' : ''}`}
      onDragOver={handleDragOver}
      onDragLeave={handleDragLeave}
      onDrop={handleDrop}
    >
      {/* Header with title */}
      <div className="file-browser-header">
        <h3>{fileBrowserTitle(source)}</h3>
        <div className="file-browser-header-actions">
          <button
            className={`hidden-toggle-button${showHidden ? ' active' : ''}`}
            onClick={() => setShowHidden(!showHidden)}
            title={showHidden ? 'Hide hidden files' : 'Show hidden files'}
            aria-pressed={showHidden}
          >
            {showHidden ? <Eye size={16} /> : <EyeOff size={16} />}
          </button>
          <button
            className="refresh-button"
            onClick={refresh}
            disabled={isInitialLoading}
            title="Refresh"
          >
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M23 4v6h-6" />
              <path d="M1 20v-6h6" />
              <path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15" />
            </svg>
          </button>
        </div>
      </div>

      {/* Navigation bar */}
      <div className="file-browser-nav">
        <button
          className="nav-button"
          onClick={goBack}
          disabled={!canGoBack || isInitialLoading}
          title="Back"
        >
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
            <path d="M19 12H5M12 19l-7-7 7-7" />
          </svg>
        </button>
        <button
          className="nav-button"
          onClick={goForward}
          disabled={!canGoForward || isInitialLoading}
          title="Forward"
        >
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
            <path d="M5 12h14M12 5l7 7-7 7" />
          </svg>
        </button>
        <button
          className="nav-button"
          onClick={goUp}
          disabled={!canGoUp || isInitialLoading}
          title="Up one level"
        >
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
            <path d="M12 19V5M5 12l7-7 7 7" />
          </svg>
        </button>
        <span className="current-path" title={currentPath}>
          {currentPath}
        </span>
      </div>

      {/* Action buttons: Upload Files + New Folder, equal width */}
      <div className="file-browser-actions">
        <input
          ref={fileInputRef}
          type="file"
          multiple
          onChange={handleFileInputChange}
          style={{ display: 'none' }}
        />
        <div className="file-browser-action-buttons">
          <button
            className="action-button upload-button"
            onClick={handleUploadClick}
            disabled={uploadProgress || isInitialLoading}
            title="Upload files"
          >
            <Upload size={14} />
            <span className="action-button-label">
              {uploadProgress
                ? uploadPercent !== null
                  ? `Uploading ${uploadPercent}%`
                  : 'Uploading...'
                : 'Upload Files'}
            </span>
          </button>
          <button
            className="action-button new-folder-button"
            onClick={handleNewFolderClick}
            disabled={uploadProgress || isInitialLoading}
            title="Create new folder"
          >
            <FolderPlus size={14} />
            <span className="action-button-label">New Folder</span>
          </button>
        </div>
        {uploadProgress && uploadPercent !== null && (
          <div className="upload-progress-bar">
            <div
              className="upload-progress-bar-fill"
              style={{ width: `${uploadPercent}%` }}
            />
          </div>
        )}
      </div>

      {/* Error display */}
      {error && (
        <div className="file-browser-error">
          {error.includes('\n') ? (
            <>
              <div className="error-summary">{error.split('\n')[0]}</div>
              <ul className="error-list">
                {error.split('\n').slice(1).map((line, i) => (
                  <li key={i}>{line}</li>
                ))}
              </ul>
            </>
          ) : (
            error
          )}
        </div>
      )}

      {/* File list */}
      <div className="file-list">
        {loading && files.length === 0 ? (
          <div className="file-browser-loading">Loading...</div>
        ) : !loading && visibleFiles.length === 0 ? (
          <div className="file-browser-empty">
            <p>No files yet</p>
            {hiddenCount > 0 ? (
              <p className="drop-hint">
                {hiddenCount} hidden item{hiddenCount !== 1 ? 's' : ''}. Toggle "Show hidden files" to reveal {hiddenCount !== 1 ? 'them' : 'it'}.
              </p>
            ) : (
              <p className="drop-hint">
                {source.kind === 'project'
                  ? 'Shared by every chat in this project; chats reach these files as proj:// paths. You can drop or upload files and folders here.'
                  : <>You can drop or upload files and folders here for the agent to work on.
                    Files created by the agent will also appear here.</>}
              </p>
            )}
          </div>
        ) : (
          visibleFiles.map((item) => {
            const iconInfo = item.type === 'file' ? getFileIconInfo(item.name) : null;
            const FileIcon = iconInfo?.Icon;
            return (
            <div
              key={item.name}
              className={`file-item ${item.type}`}
              onClick={() => handleItemClick(item)}
            >
              <div className={`file-icon${iconInfo?.className ? ` ${iconInfo.className}` : ''}`}>
                {item.type === 'folder' ? (
                  <Folder size={20} />
                ) : FileIcon ? (
                  <FileIcon size={20} />
                ) : null}
              </div>
              <div className="file-info">
                <span className="file-name" title={item.name}>
                  {item.name}
                </span>
                <span className="file-meta">
                  {item.type === 'file' && item.size !== null && (
                    <span className="file-size">{formatFileSize(item.size)}</span>
                  )}
                  <span className="file-date">{formatDate(item.lastModified)}</span>
                </span>
              </div>
              <div className="file-menu-container">
                <button
                  className="file-menu-button"
                  onClick={(e) => {
                    e.stopPropagation();
                    setOpenMenuItemName(
                      openMenuItemName === item.name ? null : item.name
                    );
                  }}
                  title="More options"
                >
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor">
                    <circle cx="12" cy="5" r="2"></circle>
                    <circle cx="12" cy="12" r="2"></circle>
                    <circle cx="12" cy="19" r="2"></circle>
                  </svg>
                </button>
                {openMenuItemName === item.name && (
                  <div className="file-menu-dropdown">
                    {item.type === 'file' && (
                      <button
                        className="file-menu-item"
                        onClick={(e) => handleDownloadClick(e, item)}
                      >
                        Download
                      </button>
                    )}
                    {item.type === 'folder' && (
                      <button
                        className="file-menu-item"
                        onClick={(e) => handleFolderDownloadClick(e, item)}
                      >
                        Download as Zip
                      </button>
                    )}
                    {rowActions?.({ entry: item, path: entryPath(item), source }).map((action) => (
                      <button
                        key={action.key}
                        className={`file-menu-item${action.danger ? ' file-menu-item-danger' : ''}`}
                        disabled={action.disabled}
                        onClick={(e) => {
                          e.stopPropagation();
                          setOpenMenuItemName(null);
                          action.onSelect();
                        }}
                      >
                        {action.label}
                      </button>
                    ))}
                    <button
                      className="file-menu-item file-menu-item-danger"
                      onClick={(e) => handleDeleteClick(e, item)}
                    >
                      Delete
                    </button>
                  </div>
                )}
              </div>
            </div>
          );})
        )}
      </div>

      {/* Zipping notification */}
      {zippingFolder && (
        <div className="file-browser-zipping">
          Zipping {zippingFolder}...
        </div>
      )}

      {/* Drag overlay */}
      {isDragOver && (
        <div className="drag-overlay">
          <p>Drop files or folders here to upload</p>
        </div>
      )}

      {/* New folder modal */}
      <NewFolderModal
        isOpen={newFolderModalOpen}
        onClose={() => setNewFolderModalOpen(false)}
        onSubmit={handleNewFolderSubmit}
      />

      {/* File viewer modal */}
      {viewerFile && (
        <FileViewerModal
          isOpen={!!viewerFile}
          source={source}
          filePath={viewerFile.path}
          fileName={viewerFile.name}
          isImage={viewerFile.isImage}
          isJson={viewerFile.isJson}
          isPdf={viewerFile.isPdf}
          isCsv={viewerFile.isCsv}
          onClose={() => setViewerFile(null)}
          onDownload={async () => {
            try {
              await downloadFile(viewerFile.path);
            } catch {
              // Error is handled in the hook
            }
          }}
        />
      )}
    </div>
  );
}
