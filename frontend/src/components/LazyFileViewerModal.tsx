/**
 * The file viewer as a lazy chunk. Hosts render `LazyFileViewerModal` with
 * FileViewerModal's props; the viewer's code (text, markdown, CSV and JSON
 * views; pdf.js sits behind a further split inside FileViewerModal) is
 * fetched the first time a file is opened. While it loads, and if the
 * chunk fails, a plain overlay stands in for the modal so the click is
 * still answered and Escape / the backdrop still close it.
 */
import type { ComponentProps, ReactNode } from 'react';
import type { FileViewerModal as FileViewerModalComponent } from './FileViewerModal';
import { LazyBoundary, lazyNamed } from './LazyBoundary';
import { ModalShell } from './ModalShell';

const FileViewerModal = lazyNamed(() => import('./FileViewerModal'), 'FileViewerModal');

export function LazyFileViewerModal(props: ComponentProps<typeof FileViewerModalComponent>) {
  const frame = (content: ReactNode) => (
    <ModalShell
      isOpen={props.isOpen}
      onClose={props.onClose}
      overlayClassName="lazy-modal-overlay"
      modalClassName="lazy-modal"
    >
      {content}
    </ModalShell>
  );
  return (
    <LazyBoundary fallback={frame(<p className="lazy-boundary-loading">Loading...</p>)} frame={frame}>
      <FileViewerModal {...props} />
    </LazyBoundary>
  );
}
