/**
 * Code-split views: `lazyNamed()` and `<LazyBoundary>`.
 *
 * Views that most page loads never open (the file viewer and pdf.js, the
 * Quest Docs editor and history, the admin pages and admin settings
 * sections) are separate chunks fetched the first time they render.
 * `lazyNamed()` is React.lazy for a named export; when the chunk cannot be
 * fetched or evaluated, the lazy component throws a `ChunkLoadError`.
 *
 * `<LazyBoundary>` wraps such a component: a Suspense boundary showing
 * `fallback` while the chunk loads, plus an error boundary that turns a
 * ChunkLoadError into a "reload" notice instead of unmounting the app. The
 * usual cause is a redeploy under a long-open tab: the new build replaced
 * the hashed chunk files the old page refers to, so /assets answers 404 and
 * only a reload (fresh index.html, fresh chunk names) recovers. The notice
 * asks instead of reloading by itself because a reload drops unsent
 * composer text. Any other render error is rethrown to the boundary above.
 */
import { Component, lazy, Suspense, type ComponentType, type ReactNode } from 'react';
import './LazyBoundary.css';

/** A code-split chunk failed to load; the original error is the `cause`. */
export class ChunkLoadError extends Error {
  constructor(cause: unknown) {
    super('A part of the app failed to load.', { cause });
    this.name = 'ChunkLoadError';
  }
}

// React.lazy's own bound; each component's props type flows through as-is.
// eslint-disable-next-line @typescript-eslint/no-explicit-any -- mirrors React.lazy's ComponentType<any>
type AnyComponent = ComponentType<any>;

/**
 * React.lazy for a named component export:
 * `lazyNamed(() => import('./FileViewerModal'), 'FileViewerModal')`.
 * Render the result inside a `<LazyBoundary>`.
 */
export function lazyNamed<K extends string, M extends { [P in K]: AnyComponent }>(
  load: () => Promise<M>,
  name: K,
) {
  return lazy(() =>
    load().then(
      (module) => ({ default: module[name] }),
      (error: unknown) => {
        throw new ChunkLoadError(error);
      },
    ),
  );
}

interface LazyBoundaryProps {
  /** Shown while the chunk loads. */
  fallback: ReactNode;
  /** Wraps the load-failure notice, e.g. in a modal overlay; inline when omitted. */
  frame?: (notice: ReactNode) => ReactNode;
  children: ReactNode;
}

interface LazyBoundaryState {
  error: unknown;
}

export class LazyBoundary extends Component<LazyBoundaryProps, LazyBoundaryState> {
  state: LazyBoundaryState = { error: null };

  static getDerivedStateFromError(error: unknown): LazyBoundaryState {
    return { error };
  }

  render() {
    const { error } = this.state;
    if (error) {
      // Not a chunk failure: let the boundary above (or React) handle it.
      if (!(error instanceof ChunkLoadError)) throw error;
      const notice = <ChunkLoadNotice />;
      return this.props.frame ? this.props.frame(notice) : notice;
    }
    return <Suspense fallback={this.props.fallback}>{this.props.children}</Suspense>;
  }
}

function ChunkLoadNotice() {
  return (
    <div className="lazy-boundary-error" role="alert">
      <p className="lazy-boundary-error-text">
        This part of the app could not be loaded. The app may have been updated since this page
        was opened.
      </p>
      <button type="button" className="lazy-boundary-reload" onClick={() => window.location.reload()}>
        Reload page
      </button>
    </div>
  );
}
