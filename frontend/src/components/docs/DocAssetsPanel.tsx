/**
 * The doc viewer's "Assets" list: every image in the doc's `assets/`
 * directory (GET /docs/{id} `assets`), one row each -- a 40px thumbnail from
 * the cookie-authed asset route, the filename and its size. A row opens the
 * image full size in DocImageLightbox (with a Download link); a thumbnail
 * that fails to load falls back to an image-file icon.
 *
 * Two presentations of the same list: `card` is the desktop aside's floating
 * card (the chat RightPanel's `.right-panel-card` chrome and uppercase
 * heading), `details` the phone's collapsed "Assets (N)" section under the
 * document. Lists every asset the server returned, including ones the body
 * no longer references.
 */

import { useState } from 'react';
import { ChevronRight, FileImage } from 'lucide-react';
import { docAssetUrl } from '../../api/docsApi';
import type { DocAsset } from '../../api/types';
import { formatDocSize } from '../../utils/allDocsGrouping';
import { DocImageLightbox } from './DocImageLightbox';
import '../RightPanel.css';
import './DocAssetsPanel.css';

export interface DocAssetsPanelProps {
  docId: string;
  assets: DocAsset[];
  /** `card` (default): desktop aside card. `details`: phone collapsed section. */
  variant?: 'card' | 'details';
}

/** One asset's size, e.g. "340 B", "1.2 KB" (formatDocSize without images). */
function formatAssetSize(size: number): string {
  return formatDocSize(size, 0);
}

export function DocAssetsPanel({ docId, assets, variant = 'card' }: DocAssetsPanelProps) {
  const [openAsset, setOpenAsset] = useState<DocAsset | null>(null);
  // Thumbnail URLs that failed to load (icon instead). Keyed by URL, so a
  // re-uploaded asset under a new doc id / name retries.
  const [failedUrls, setFailedUrls] = useState<ReadonlySet<string>>(() => new Set());

  const markFailed = (url: string) => {
    setFailedUrls((prev) => {
      if (prev.has(url)) return prev;
      const next = new Set(prev);
      next.add(url);
      return next;
    });
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
            <li key={asset.name}>
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
            </li>
          );
        })}
      </ul>
    );

  const lightbox = (
    <DocImageLightbox docId={docId} asset={openAsset} onClose={() => setOpenAsset(null)} />
  );

  if (variant === 'details') {
    return (
      <details className="doc-assets-details">
        <summary className="doc-assets-summary">
          <ChevronRight size={14} className="doc-assets-summary-chevron" aria-hidden="true" />
          <span>Assets ({assets.length})</span>
        </summary>
        <div className="doc-assets-details-body">{list}</div>
        {lightbox}
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
      {lightbox}
    </section>
  );
}
