/**
 * Full-size view of one doc image, opened from the viewer's Assets list.
 * A ModalShell dialog (backdrop click / Escape close it) with the filename,
 * its size and a Download link over the image on a checkerboard ground.
 * The image and the download both use the cookie-authed asset route
 * (GET /docs/{id}/assets/{name}); FileViewerModal is not reused because it
 * fetches through a conversation's workspace files route.
 */

import { useState } from 'react';
import { Download, ImageOff, X } from 'lucide-react';
import { docAssetUrl } from '../../api/docsApi';
import type { DocAsset } from '../../api/types';
import { formatDocSize } from '../../utils/allDocsGrouping';
import { ModalShell } from '../ModalShell';
import './DocImageLightbox.css';

export interface DocImageLightboxProps {
  docId: string;
  /** The asset to show; null = closed. */
  asset: DocAsset | null;
  onClose: () => void;
}

export function DocImageLightbox({ docId, asset, onClose }: DocImageLightboxProps) {
  const url = asset ? docAssetUrl(docId, asset.name) : null;
  // The URL that failed to load (a later asset retries).
  const [failedUrl, setFailedUrl] = useState<string | null>(null);

  return (
    <ModalShell
      isOpen={asset !== null}
      onClose={onClose}
      overlayClassName="doc-lightbox-overlay"
      modalClassName="doc-lightbox"
    >
      {asset && url && (
        <>
          <div className="doc-lightbox-header">
            <div className="doc-lightbox-title-group">
              <h2 className="doc-lightbox-title" title={asset.name}>
                {asset.name}
              </h2>
              <span className="doc-lightbox-size">{formatDocSize(asset.size, 0)}</span>
            </div>
            <a className="doc-lightbox-download" href={url} download={asset.name}>
              <Download size={14} aria-hidden="true" />
              Download
            </a>
            <button
              type="button"
              className="doc-lightbox-close"
              onClick={onClose}
              aria-label="Close"
            >
              <X size={18} aria-hidden="true" />
            </button>
          </div>
          <div className="doc-lightbox-stage">
            {failedUrl === url ? (
              <div className="doc-lightbox-error">
                <ImageOff size={28} aria-hidden="true" />
                <p>This image could not be loaded.</p>
              </div>
            ) : (
              <img
                className="doc-lightbox-image"
                src={url}
                alt={asset.name}
                onError={() => setFailedUrl(url)}
              />
            )}
          </div>
        </>
      )}
    </ModalShell>
  );
}
