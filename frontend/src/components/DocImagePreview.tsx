/**
 * Image preview shown inside write_doc `add_image` approval cards (preview
 * field type 'doc_image'). The image is still a workspace file of the
 * proposing conversation at card time (project conversations share the
 * project workspace, so the card's conversation id always resolves it), so
 * the thumbnail and the full-size viewer go through that conversation's
 * cookie-authed files/download endpoint. Rendered by both card renderers
 * (ActionRequestMessage and RequestsView) via ActionRequestPreviewFields.
 */

import { useCallback, useState } from 'react';
import { API_BASE_URL } from '../api/config';
import { downloadFile } from '../api/fileApi';
import type { DocImagePreview as DocImagePreviewData } from '../api/types';
import { FileViewerModal } from './FileViewerModal';
import './DocImagePreview.css';

interface DocImagePreviewProps {
  image: DocImagePreviewData;
  conversationId: string;
  /** False for placement "none" (stored as an asset, not appended): the
   *  markdown line is then shown as a reference, not as "Appended". */
  placed?: boolean;
}

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function DocImagePreview({ image, conversationId, placed = true }: DocImagePreviewProps) {
  const path = image.workspace_path;
  const fileName = path.split('/').pop() || path;
  const url = `${API_BASE_URL}/conversations/${encodeURIComponent(conversationId)}`
    + `/files/download?path=${encodeURIComponent(path)}`;
  // Track the failing URL rather than a boolean so a re-render with a new
  // image (card refreshed from the API) retries.
  const [failedUrl, setFailedUrl] = useState<string | null>(null);
  const [viewerOpen, setViewerOpen] = useState(false);

  const handleDownload = useCallback(async () => {
    try {
      const { url: blobUrl, filename } = await downloadFile(conversationId, path);
      const a = document.createElement('a');
      a.href = blobUrl;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      setTimeout(() => URL.revokeObjectURL(blobUrl), 0);
    } catch {
      // Swallow; the user can retry.
    }
  }, [conversationId, path]);

  const failed = failedUrl === url;

  return (
    <div className="doc-image-preview">
      {failed ? (
        <code className="doc-image-preview-missing" title={path}>{path}</code>
      ) : (
        <button
          type="button"
          className="doc-image-preview-thumb"
          onClick={() => setViewerOpen(true)}
          title={`Open ${fileName}`}
        >
          <img
            src={url}
            alt={image.asset_name}
            loading="lazy"
            onError={() => setFailedUrl(url)}
          />
        </button>
      )}
      <div className="doc-image-preview-caption">
        <span className="doc-image-preview-name">{image.asset_name}</span>
        {image.size_bytes != null && (
          <span className="doc-image-preview-size">{formatSize(image.size_bytes)}</span>
        )}
        <span className="doc-image-preview-stored">
          Stored as <code>assets/{image.asset_name}</code>
        </span>
      </div>
      {image.markdown && (
        <div className="doc-image-preview-markdown">
          <span className="doc-image-preview-markdown-label">
            {placed ? 'Appended:' : 'Markdown:'}
          </span>
          <code className="doc-image-preview-markdown-code">{image.markdown}</code>
        </div>
      )}
      {viewerOpen && (
        <FileViewerModal
          isOpen={viewerOpen}
          conversationId={conversationId}
          filePath={path}
          fileName={fileName}
          // The server validated the file as an image by its magic bytes,
          // so the extension check FileBrowser's isImageFile does is moot.
          isImage
          onClose={() => setViewerOpen(false)}
          onDownload={() => void handleDownload()}
        />
      )}
    </div>
  );
}
