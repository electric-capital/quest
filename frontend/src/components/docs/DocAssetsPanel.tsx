/**
 * The doc viewer's "Assets" list: every image in the doc's `assets/`
 * directory (GET /docs/{id} `assets`), one row each -- a 40px thumbnail from
 * the cookie-authed asset route, the filename and its size. A row opens the
 * image full size in DocImageLightbox (with a Download link); a thumbnail
 * that fails to load falls back to an image-file icon.
 *
 * With `canDelete` (the owner, in view mode) each row also carries a trash
 * button: a DocConfirmDialog, then DELETE /docs/{id}/assets/{name}, then
 * `onDeleted` so the viewer re-fetches the doc. The server refuses (409
 * `asset_in_use`) while the current body still references the image; that
 * and any other failure shows inside the dialog.
 *
 * Two presentations of the same list: `card` is the desktop aside's floating
 * card (the chat RightPanel's `.right-panel-card` chrome and uppercase
 * heading), `details` the phone's collapsed "Assets (N)" section under the
 * document. Lists every asset the server returned, including ones the body
 * no longer references.
 */

import { useState } from 'react';
import { ChevronRight, FileImage, Trash2 } from 'lucide-react';
import { deleteDocAsset, docAssetUrl } from '../../api/docsApi';
import { ApiClientError } from '../../api/request';
import type { DocAsset, DocAssetDeleteResponse } from '../../api/types';
import { formatDocSize } from '../../utils/allDocsGrouping';
import { DocConfirmDialog } from './DocConfirmDialog';
import { DocImageLightbox } from './DocImageLightbox';
import '../RightPanel.css';
import './DocAssetsPanel.css';

const ASSET_IN_USE_MESSAGE = 'This image is used in the doc. Remove it from the text first.';

export interface DocAssetsPanelProps {
  docId: string;
  assets: DocAsset[];
  /** `card` (default): desktop aside card. `details`: phone collapsed section. */
  variant?: 'card' | 'details';
  /** Offer a Delete button per image (the owner, in view mode). */
  canDelete?: boolean;
  /** An image was deleted; the viewer re-fetches the doc's asset list. */
  onDeleted?: (name: string, result: DocAssetDeleteResponse) => void;
}

/** One asset's size, e.g. "340 B", "1.2 KB" (formatDocSize without images). */
function formatAssetSize(size: number): string {
  return formatDocSize(size, 0);
}

function deleteErrorMessage(err: unknown): string {
  if (err instanceof ApiClientError && err.errorCode === 'asset_in_use') return ASSET_IN_USE_MESSAGE;
  return err instanceof Error && err.message ? err.message : 'Failed to delete the image.';
}

export function DocAssetsPanel({
  docId,
  assets,
  variant = 'card',
  canDelete = false,
  onDeleted,
}: DocAssetsPanelProps) {
  const [openAsset, setOpenAsset] = useState<DocAsset | null>(null);
  // Thumbnail URLs that failed to load (icon instead). Keyed by URL, so a
  // re-uploaded asset under a new doc id / name retries.
  const [failedUrls, setFailedUrls] = useState<ReadonlySet<string>>(() => new Set());
  // The image the delete confirm is open for.
  const [deleteTarget, setDeleteTarget] = useState<DocAsset | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);

  const markFailed = (url: string) => {
    setFailedUrls((prev) => {
      if (prev.has(url)) return prev;
      const next = new Set(prev);
      next.add(url);
      return next;
    });
  };

  const openDelete = (asset: DocAsset) => {
    setDeleteError(null);
    setDeleteTarget(asset);
  };

  const confirmDelete = async () => {
    if (!deleteTarget) return;
    const { name } = deleteTarget;
    setDeleting(true);
    setDeleteError(null);
    try {
      const result = await deleteDocAsset(docId, name);
      setDeleting(false);
      setDeleteTarget(null);
      onDeleted?.(name, result);
    } catch (err) {
      setDeleting(false);
      setDeleteError(deleteErrorMessage(err));
    }
  };

  const list =
    assets.length === 0 ? (
      <p className="doc-assets-empty">No images in this doc.</p>
    ) : (
      <ul className="doc-assets-list">
        {assets.map((asset) => {
          const url = docAssetUrl(docId, asset.name);
          const size = formatAssetSize(asset.size);
          return (
            <li key={asset.name} className={canDelete ? 'doc-asset-item doc-asset-item--deletable' : 'doc-asset-item'}>
              <button
                type="button"
                className="doc-asset-row"
                onClick={() => setOpenAsset(asset)}
                title={`${asset.name} (${size})`}
              >
                <span className="doc-asset-thumb" aria-hidden="true">
                  {failedUrls.has(url) ? (
                    <FileImage size={18} className="doc-asset-thumb-icon" />
                  ) : (
                    <img src={url} alt="" loading="lazy" onError={() => markFailed(url)} />
                  )}
                </span>
                <span className="doc-asset-name">{asset.name}</span>
                <span className="doc-asset-size">{size}</span>
              </button>
              {canDelete && (
                <button
                  type="button"
                  className="doc-asset-delete"
                  onClick={() => openDelete(asset)}
                  aria-label={`Delete ${asset.name}`}
                  title={`Delete ${asset.name}`}
                >
                  <Trash2 size={15} aria-hidden="true" />
                </button>
              )}
            </li>
          );
        })}
      </ul>
    );

  const dialogs = (
    <>
      <DocImageLightbox docId={docId} asset={openAsset} onClose={() => setOpenAsset(null)} />
      {canDelete && (
        <DocConfirmDialog
          isOpen={deleteTarget !== null}
          title={`Delete image '${deleteTarget?.name ?? ''}'?`}
          confirmLabel="Delete"
          busyLabel="Deleting..."
          tone="danger"
          busy={deleting}
          error={deleteError}
          onConfirm={() => void confirmDelete()}
          onClose={() => setDeleteTarget(null)}
        >
          <p>
            It is removed from this doc's assets. Older versions in History that used it
            will show a missing image.
          </p>
        </DocConfirmDialog>
      )}
    </>
  );

  if (variant === 'details') {
    return (
      <details className="doc-assets-details">
        <summary className="doc-assets-summary">
          <ChevronRight size={14} className="doc-assets-summary-chevron" aria-hidden="true" />
          <span>Assets ({assets.length})</span>
        </summary>
        <div className="doc-assets-details-body">{list}</div>
        {dialogs}
      </details>
    );
  }

  return (
    <section className="right-panel-card doc-assets-panel" aria-label="Assets">
      <div className="doc-assets-header">
        <h3 className="doc-assets-heading">Assets</h3>
        <span className="doc-assets-count">{assets.length}</span>
      </div>
      <div className="doc-assets-scroll">{list}</div>
      {dialogs}
    </section>
  );
}
