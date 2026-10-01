/**
 * Modal for viewing text file content, images, and PDFs from workspace files.
 */

import { useState, useEffect, useCallback, useRef, useMemo } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import remarkBreaks from 'remark-breaks';
import rehypeHighlight from 'rehype-highlight';
import remarkMath from 'remark-math';
import rehypeKatex from 'rehype-katex';
import remarkMathCurrencyGuard from '../utils/remarkMathCurrencyGuard';
import { errorMessage } from '../utils/errorMessage';
import { fetchFileContent, saveFileToDrive } from '../api/fileApi';
import { API_BASE_URL } from '../api/config';
import { JsonTreeViewer } from './JsonTreeViewer';
import { PdfViewer } from './PdfViewer';
import { markdownComponents, MarkdownWorkspaceContext } from './Message';
import { parseCsv } from '../utils/csv';
import { ModalShell } from './ModalShell';
import './FileViewerModal.css';

// PDFs are fetched whole into memory before handing to pdf.js; refuse
// pathological sizes (workspace upload cap is 200 MB) and point at Download.
const PDF_MAX_PREVIEW_BYTES = 100 * 1024 * 1024;

// Cap on CSV rows rendered into the table. The 5 MB server-side view limit
// still applies, but a 5 MB CSV can be tens of thousands of rows; cap the DOM
// and show a "use Download for the full file" note when exceeded.
const CSV_MAX_RENDER_ROWS = 5000;
// Long-cell truncation (mirrors TableViewerModal's MAX_CELL_LENGTH).
const CSV_MAX_CELL_LENGTH = 200;

function isMarkdownFile(fileName: string): boolean {
  return fileName.toLowerCase().endsWith('.md');
}

function computeDefaultDocName(fileName: string): string {
  // Remove .md extension, replace underscores with spaces
  return fileName.replace(/\.md$/i, '').replace(/_/g, ' ');
}

function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(1)} GB`;
}

interface FileViewerModalProps {
  isOpen: boolean;
  conversationId: string;
  filePath: string;       // Full path within workspace (e.g., "/script.py")
  fileName: string;       // Display name
  isImage?: boolean;      // Whether the file is an image (renders via <img> instead of <pre>)
  isJson?: boolean;       // Whether the file is JSON (renders via JsonTreeViewer instead of <pre>)
  isPdf?: boolean;        // Whether the file is a PDF (renders via PdfViewer instead of <pre>)
  isCsv?: boolean;        // Whether the file is CSV (renders as a styled sortable table instead of <pre>)
  onClose: () => void;
  onDownload: () => void; // Triggers the existing download flow
}

export function FileViewerModal({ isOpen, conversationId, filePath, fileName, isImage, isJson, isPdf, isCsv, onClose, onDownload }: FileViewerModalProps) {
  const [content, setContent] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [imageDimensions, setImageDimensions] = useState<{ width: number; height: number } | null>(null);
  const [fileSize, setFileSize] = useState<string | null>(null);
  const [imageObjectUrl, setImageObjectUrl] = useState<string | null>(null);
  const [pdfData, setPdfData] = useState<ArrayBuffer | null>(null);
  const [pdfPages, setPdfPages] = useState<number | null>(null);

  // Markdown preview view mode: rendered HTML (default, switch ON) vs. raw source (switch OFF).
  const [showRendered, setShowRendered] = useState(true);

  // Resolves workspace-relative markdown image refs in the .md preview.
  const markdownCtx = useMemo(() => ({ conversationId }), [conversationId]);

  // CSV preview view mode: styled table (default, switch OFF) vs. raw source (switch ON).
  const [showRaw, setShowRaw] = useState(false);
  // Client-side sort state for the CSV table (CSV has no backend query layer,
  // unlike TableViewerModal which sorts server-side). null = unsorted (file order).
  const [csvSortBy, setCsvSortBy] = useState<number | null>(null);
  const [csvSortDir, setCsvSortDir] = useState<'asc' | 'desc'>('asc');

  // Save to Google Drive dialog state
  const [saveToDriveOpen, setSaveToDriveOpen] = useState(false);
  const [docName, setDocName] = useState('');
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [saveSuccess, setSaveSuccess] = useState<string | null>(null);
  const docNameInputRef = useRef<HTMLInputElement>(null);

  // Fetch file content when modal opens
  useEffect(() => {
    if (!isOpen) {
      setContent(null);
      setError(null);
      setImageDimensions(null);
      setFileSize(null);
      setPdfData(null);
      setPdfPages(null);
      if (imageObjectUrl) {
        URL.revokeObjectURL(imageObjectUrl);
        setImageObjectUrl(null);
      }
      setShowRendered(true);
      setShowRaw(false);
      setCsvSortBy(null);
      setCsvSortDir('asc');
      return;
    }

    let cancelled = false;
    setLoading(true);
    setError(null);
    // Each newly-opened file starts in the default rendered view.
    setShowRendered(true);
    // CSV defaults to the styled table (raw off) and unsorted (file order).
    setShowRaw(false);
    setCsvSortBy(null);
    setCsvSortDir('asc');

    if (isImage) {
      // Fetch image as a blob to get accurate file size and create an object URL
      const url = `${API_BASE_URL}/conversations/${conversationId}/files/download?path=${encodeURIComponent(filePath)}`;
      fetch(url)
        .then((res) => {
          if (!res.ok) throw new Error('Failed to load image');
          return res.blob();
        })
        .then((blob) => {
          if (cancelled) return;
          setFileSize(formatFileSize(blob.size));
          const objectUrl = URL.createObjectURL(blob);
          setImageObjectUrl(objectUrl);
          // Loading state will be cleared by the <img> onLoad handler
        })
        .catch((err) => {
          if (!cancelled) {
            setError(err.message || 'Failed to load image');
            setLoading(false);
          }
        });
    } else if (isPdf) {
      // Fetch the whole PDF as bytes and hand it to PdfViewer (the /files/content
      // text endpoint is restricted to text extensions; PDFs use /files/download).
      const url = `${API_BASE_URL}/conversations/${conversationId}/files/download?path=${encodeURIComponent(filePath)}`;
      const tooLargeMessage = (bytes: number) =>
        `This PDF is too large to preview (${formatFileSize(bytes)}). Use Download instead.`;
      fetch(url)
        .then((res) => {
          if (!res.ok) throw new Error('Failed to load file');
          const contentLength = Number(res.headers.get('Content-Length') || 0);
          if (contentLength > PDF_MAX_PREVIEW_BYTES) {
            throw new Error(tooLargeMessage(contentLength));
          }
          return res.arrayBuffer();
        })
        .then((buffer) => {
          if (cancelled) return;
          if (buffer.byteLength > PDF_MAX_PREVIEW_BYTES) {
            setError(tooLargeMessage(buffer.byteLength));
            setLoading(false);
            return;
          }
          // Capture the size before handoff (PdfViewer copies the buffer for
          // pdf.js, but never depend on it staying usable here).
          setFileSize(formatFileSize(buffer.byteLength));
          setPdfData(buffer);
          setLoading(false);
        })
        .catch((err) => {
          if (!cancelled) {
            setError(err.message || 'Failed to load file');
            setLoading(false);
          }
        });
    } else {
      fetchFileContent(conversationId, filePath)
        .then((data) => {
          if (!cancelled) {
            setContent(data.content);
            setLoading(false);
          }
        })
        .catch((err) => {
          if (!cancelled) {
            setError(err.message || 'Failed to load file');
            setLoading(false);
          }
        });
    }

    return () => {
      cancelled = true;
    };
  }, [isOpen, conversationId, filePath, isImage, isPdf]);

  const handlePdfMetadata = useCallback((info: { numPages: number }) => {
    setPdfPages(info.numPages);
  }, []);

  // Parse CSV once per loaded content (parseCsv never throws; on failure it
  // returns empty columns so the render branch falls back to the raw <pre>).
  const parsedCsv = useMemo(() => {
    if (!isCsv || content === null) return { columns: [], rows: [] };
    return parseCsv(content);
  }, [isCsv, content]);

  // Apply the client-side sort to the parsed rows. Numeric-aware comparator so
  // "10" sorts after "9"; empty cells sort last (asc) / consistently.
  const sortedCsvRows = useMemo(() => {
    const { rows } = parsedCsv;
    if (csvSortBy === null) return rows;
    const col = csvSortBy;
    const dir = csvSortDir === 'asc' ? 1 : -1;
    const compare = (a: string[], b: string[]): number => {
      const av = a[col] ?? '';
      const bv = b[col] ?? '';
      // Empty cells always sort to the bottom regardless of direction.
      if (av === '' && bv === '') return 0;
      if (av === '') return 1;
      if (bv === '') return -1;
      const an = Number(av);
      const bn = Number(bv);
      if (!Number.isNaN(an) && !Number.isNaN(bn)) {
        return (an - bn) * dir;
      }
      return av.localeCompare(bv) * dir;
    };
    // Copy before sorting so the memoized parse output stays stable (file order).
    return [...rows].sort(compare);
  }, [parsedCsv, csvSortBy, csvSortDir]);

  // Three-state column-header sort cycle: none -> asc -> desc -> none
  // (mirrors TableViewerModal.handleSort, but operating on a column index).
  const handleCsvSort = useCallback((col: number) => {
    if (csvSortBy !== col) {
      setCsvSortBy(col);
      setCsvSortDir('asc');
      return;
    }
    if (csvSortDir === 'asc') {
      setCsvSortDir('desc');
    } else {
      setCsvSortBy(null);
      setCsvSortDir('asc');
    }
  }, [csvSortBy, csvSortDir]);

  // Image load/error handlers
  const handleImageLoad = useCallback((e: React.SyntheticEvent<HTMLImageElement>) => {
    const img = e.currentTarget;
    if (img.naturalWidth && img.naturalHeight) {
      setImageDimensions({ width: img.naturalWidth, height: img.naturalHeight });
    }
    setLoading(false);
    setError(null);
  }, []);

  const handleImageError = useCallback(() => {
    setLoading(false);
    setError('Failed to load image');
  }, []);

  const handleOpenSaveToDrive = useCallback(() => {
    setDocName(computeDefaultDocName(fileName));
    setSaveError(null);
    setSaveSuccess(null);
    setSaveToDriveOpen(true);
    // Auto-select the input text after the dialog renders
    setTimeout(() => {
      docNameInputRef.current?.select();
    }, 0);
  }, [fileName]);

  const handleCloseSaveToDrive = useCallback(() => {
    if (!saving) {
      setSaveToDriveOpen(false);
      setSaveError(null);
      setSaveSuccess(null);
    }
  }, [saving]);

  const handleSaveToDrive = useCallback(async () => {
    if (!docName.trim()) {
      setSaveError('Document name cannot be empty');
      return;
    }
    setSaving(true);
    setSaveError(null);
    try {
      const result = await saveFileToDrive(conversationId, filePath, docName.trim());
      setSaveSuccess(result.url);
    } catch (err) {
      setSaveError(errorMessage(err, 'Failed to save to Google Drive'));
    } finally {
      setSaving(false);
    }
  }, [conversationId, filePath, docName]);

  // Handle Escape in save-to-drive dialog (prevent it from closing the main modal)
  useEffect(() => {
    if (!saveToDriveOpen) return;
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.stopPropagation();
        handleCloseSaveToDrive();
      }
    };
    document.addEventListener('keydown', handleKeyDown, true);
    return () => document.removeEventListener('keydown', handleKeyDown, true);
  }, [saveToDriveOpen, handleCloseSaveToDrive]);

  // Shared error state: a descriptive message plus a Download affordance so the
  // "use Download" guidance (e.g. for oversized files) is directly actionable.
  const renderError = (message: string) => (
    <div className="file-viewer-error">
      <svg
        className="file-viewer-error-icon"
        width="32"
        height="32"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
        aria-hidden="true"
      >
        <path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z" />
        <line x1="12" y1="9" x2="12" y2="13" />
        <line x1="12" y1="17" x2="12.01" y2="17" />
      </svg>
      <div className="file-viewer-error-message">{message}</div>
      <button className="file-viewer-error-download" onClick={onDownload}>
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
          <polyline points="7 10 12 15 17 10" />
          <line x1="12" y1="15" x2="12" y2="3" />
        </svg>
        Download file
      </button>
    </div>
  );

  return (
    <ModalShell isOpen={isOpen} onClose={onClose} overlayClassName="file-viewer-overlay" modalClassName="file-viewer-modal">
      <div className="file-viewer-header">
        <div className="file-viewer-title-group">
          <h2 className="file-viewer-title" title={fileName}>{fileName}</h2>
          {isImage && (imageDimensions || fileSize) && (
            <span className="file-viewer-image-meta">
              {imageDimensions && `${imageDimensions.width} \u00d7 ${imageDimensions.height}`}
              {imageDimensions && fileSize && ' \u00b7 '}
              {fileSize}
            </span>
          )}
          {isPdf && (pdfPages !== null || fileSize) && (
            <span className="file-viewer-image-meta">
              {pdfPages !== null && `${pdfPages} page${pdfPages !== 1 ? 's' : ''}`}
              {pdfPages !== null && fileSize && ' \u00b7 '}
              {fileSize}
            </span>
          )}
          <button className="file-viewer-download-button" onClick={onDownload}>
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
              <polyline points="7 10 12 15 17 10" />
              <line x1="12" y1="15" x2="12" y2="3" />
            </svg>
            Download
          </button>
          {isMarkdownFile(fileName) && (
            <button className="file-viewer-gdrive-button" onClick={handleOpenSaveToDrive}>
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
                <polyline points="14 2 14 8 20 8" />
                <path d="M12 18v-6" />
                <path d="M9 15l3-3 3 3" />
              </svg>
              Save to Drive
            </button>
          )}
          {isMarkdownFile(fileName) && (
            <label className="file-viewer-md-switch">
              <input
                type="checkbox"
                className="file-viewer-md-switch-input"
                checked={showRendered}
                onChange={(e) => setShowRendered(e.target.checked)}
              />
              <span className="file-viewer-md-switch-track" aria-hidden="true">
                <span className="file-viewer-md-switch-knob" />
              </span>
              <span className="file-viewer-md-switch-label">Show rendered</span>
            </label>
          )}
          {isCsv && (
            <label className="file-viewer-md-switch">
              <input
                type="checkbox"
                className="file-viewer-md-switch-input"
                checked={showRaw}
                onChange={(e) => setShowRaw(e.target.checked)}
              />
              <span className="file-viewer-md-switch-track" aria-hidden="true">
                <span className="file-viewer-md-switch-knob" />
              </span>
              <span className="file-viewer-md-switch-label">Show raw</span>
            </label>
          )}
        </div>
        <button className="file-viewer-close-button" onClick={onClose}>
          <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <line x1="18" y1="6" x2="6" y2="18"></line>
            <line x1="6" y1="6" x2="18" y2="18"></line>
          </svg>
        </button>
      </div>
      <div className={`file-viewer-content${isPdf ? ' file-viewer-content--pdf' : ''}`}>
        {isPdf ? (
          <>
            {loading && <div className="file-viewer-loading">Loading...</div>}
            {error && renderError(error)}
            {!loading && !error && pdfData && (
              <PdfViewer data={pdfData} fileName={fileName} onMetadata={handlePdfMetadata} />
            )}
          </>
        ) : isImage ? (
          <>
            {loading && <div className="file-viewer-loading">Loading...</div>}
            {error && renderError(error)}
            {!error && imageObjectUrl && (
              <div className={`file-viewer-image-container ${loading ? 'file-viewer-image-hidden' : ''}`}>
                <img
                  className="file-viewer-image"
                  src={imageObjectUrl}
                  alt={fileName}
                  onLoad={handleImageLoad}
                  onError={handleImageError}
                />
              </div>
            )}
          </>
        ) : (
          <>
            {loading ? (
              <div className="file-viewer-loading">Loading...</div>
            ) : error ? (
              renderError(error)
            ) : isMarkdownFile(fileName) && showRendered ? (
              <div className="file-viewer-markdown">
                {/* Workspace-relative image refs in .md files resolve against
                    this conversation's workspace (no click-to-open here). */}
                <MarkdownWorkspaceContext.Provider value={markdownCtx}>
                  <ReactMarkdown
                    remarkPlugins={[remarkGfm, remarkBreaks, remarkMath, remarkMathCurrencyGuard]}
                    rehypePlugins={[rehypeHighlight, rehypeKatex]}
                    components={markdownComponents}
                  >
                    {content || ''}
                  </ReactMarkdown>
                </MarkdownWorkspaceContext.Provider>
              </div>
            ) : isCsv && !showRaw && parsedCsv.columns.length > 0 ? (
              (() => {
                const totalRows = sortedCsvRows.length;
                const visibleRows = sortedCsvRows.slice(0, CSV_MAX_RENDER_ROWS);
                const truncated = totalRows > CSV_MAX_RENDER_ROWS;
                return (
                  <>
                    <div className="file-viewer-csv-wrapper">
                      <table className="file-viewer-csv-table">
                        <thead>
                          <tr>
                            {parsedCsv.columns.map((col, colIdx) => {
                              const isActive = csvSortBy === colIdx;
                              const arrow = !isActive ? '' : csvSortDir === 'asc' ? ' ▲' : ' ▼';
                              return (
                                <th
                                  key={colIdx}
                                  className={`sortable${isActive ? ' sorted' : ''}`}
                                  onClick={() => handleCsvSort(colIdx)}
                                  title={`Sort by ${col}`}
                                >
                                  {col}{arrow}
                                </th>
                              );
                            })}
                          </tr>
                        </thead>
                        <tbody>
                          {visibleRows.map((row, rowIdx) => (
                            <tr key={rowIdx}>
                              {parsedCsv.columns.map((_, cellIdx) => {
                                const cell = row[cellIdx] ?? '';
                                const isLong = cell.length > CSV_MAX_CELL_LENGTH;
                                const display = isLong ? cell.substring(0, CSV_MAX_CELL_LENGTH) + '...' : cell;
                                return (
                                  <td key={cellIdx} title={isLong ? cell : undefined}>
                                    {display}
                                  </td>
                                );
                              })}
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                    {totalRows === 0 ? (
                      <div className="file-viewer-csv-note">No data rows.</div>
                    ) : truncated ? (
                      <div className="file-viewer-csv-note">
                        Showing first {CSV_MAX_RENDER_ROWS.toLocaleString()} of {totalRows.toLocaleString()} rows &mdash; use Download for the full file.
                      </div>
                    ) : null}
                  </>
                );
              })()
            ) : isJson && content ? (
              (() => {
                try {
                  const parsed = JSON.parse(content);
                  return <JsonTreeViewer data={parsed} />;
                } catch {
                  return <pre className="file-viewer-pre">{content}</pre>;
                }
              })()
            ) : (
              <pre className="file-viewer-pre">{content}</pre>
            )}
          </>
        )}
      </div>
      {saveToDriveOpen && (
        <div className="save-to-drive-overlay" onClick={(e) => { if (e.target === e.currentTarget) handleCloseSaveToDrive(); }}>
          <div className="save-to-drive-dialog">
            <h3 className="save-to-drive-title">Save to Google Drive</h3>
            {saveSuccess ? (
              <div className="save-to-drive-success">
                <span>Saved successfully!</span>
                <a href={saveSuccess} target="_blank" rel="noopener noreferrer" className="save-to-drive-link">
                  Open in Google Docs
                </a>
                <div className="save-to-drive-actions">
                  <button className="save-to-drive-btn save-to-drive-btn-secondary" onClick={handleCloseSaveToDrive}>
                    Close
                  </button>
                </div>
              </div>
            ) : (
              <>
                <label className="save-to-drive-label">Document name</label>
                <input
                  ref={docNameInputRef}
                  className="save-to-drive-input"
                  type="text"
                  value={docName}
                  onChange={(e) => setDocName(e.target.value)}
                  onKeyDown={(e) => { if (e.key === 'Enter' && !saving) handleSaveToDrive(); }}
                  disabled={saving}
                  autoFocus
                />
                {saveError && <div className="save-to-drive-error">{saveError}</div>}
                <div className="save-to-drive-actions">
                  <button
                    className="save-to-drive-btn save-to-drive-btn-secondary"
                    onClick={handleCloseSaveToDrive}
                    disabled={saving}
                  >
                    Cancel
                  </button>
                  <button
                    className="save-to-drive-btn save-to-drive-btn-primary"
                    onClick={handleSaveToDrive}
                    disabled={saving}
                  >
                    {saving ? 'Saving...' : 'Save'}
                  </button>
                </div>
              </>
            )}
          </div>
        </div>
      )}
    </ModalShell>
  );
}
