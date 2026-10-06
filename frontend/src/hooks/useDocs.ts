/**
 * Data layer for a Quest Docs list: the user's own user docs (no
 * `projectId`), one project's docs, or the docs other people shared with the
 * user (`shared: true`, its own keyset stream; All Docs' "Shared with you").
 * Keyset-paged like the sidebar conversation list -- first
 * page on mount / whenever the arguments change, `loadMore` appends the next
 * page, `refresh` silently re-fetches the loaded window -- and kept current by
 * the `doc_list_changed` realtime global.
 *
 * Cheap by design: the Sidebar mounts two instances (limit 5) and the All
 * Docs view two more (its own list + the shared one), so a closed gate
 * (`enabled: false`) fetches nothing, every superseded response is dropped
 * instead of re-rendering, and realtime refreshes are coalesced: a burst of
 * `doc_list_changed` events (e.g. a conversation writing a doc shared with
 * everyone, which reaches every connected user) costs one re-fetch per list,
 * DOCS_REFRESH_DEBOUNCE_MS after the last event.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { DOCS_MAX_PAGE_SIZE, fetchDocs, type FetchDocsOptions } from '../api/docsApi';
import { ApiClientError } from '../api/request';
import type { Doc } from '../api/types';
import { persistentWebSocket } from '../services/PersistentWebSocket';

/** Page size when the caller passes no `limit`. */
export const DOCS_DEFAULT_PAGE_SIZE = 50;

/** Quiet period after the last `doc_list_changed` before the re-fetch. */
export const DOCS_REFRESH_DEBOUNCE_MS = 300;

export interface UseDocsOptions {
  /** A project's docs; omitted / null = the user's own user docs. */
  projectId?: string | null;
  /**
   * The docs other people shared with the user (user and project docs).
   * Takes precedence: `projectId` is ignored while this is set.
   */
  shared?: boolean;
  /** Page size (clamped to 1..200). */
  limit?: number;
  /** False (gate closed, nothing to show) = no fetch and an empty list. */
  enabled?: boolean;
}

export interface DocsList {
  docs: Doc[];
  // True only until the first response for the current arguments lands
  // (never during a silent refresh).
  loading: boolean;
  loadingMore: boolean;
  // Message of a failed first load; cleared by the next successful fetch.
  error: string | null;
  hasMore: boolean;
  loadMore: () => void;
  // Re-fetch the loaded window in one request, keeping the list on screen.
  refresh: () => Promise<void>;
}

/** What has been loaded, tagged with the arguments it was loaded for. */
interface ListState {
  key: string | null;
  docs: Doc[];
  nextCursor: string | null;
  error: string | null;
}

const EMPTY: ListState = { key: null, docs: [], nextCursor: null, error: null };

export function useDocs({
  projectId = null,
  shared = false,
  limit = DOCS_DEFAULT_PAGE_SIZE,
  enabled = true,
}: UseDocsOptions = {}): DocsList {
  const pageSize = Math.min(DOCS_MAX_PAGE_SIZE, Math.max(1, Math.floor(limit)));
  const project = shared ? null : projectId || null;
  // Identity of the list being shown; null while disabled. The stream tag
  // keeps the shared list apart from every project id.
  const key = enabled ? `${shared ? 'shared' : `project:${project ?? ''}`}|${pageSize}` : null;
  // The list selector sent with every page request of this stream.
  const streamOpts = useMemo<FetchDocsOptions>(
    () => (shared ? { shared: true } : { projectId: project }),
    [shared, project],
  );

  const [state, setState] = useState<ListState>(EMPTY);
  const [loadingMore, setLoadingMore] = useState(false);

  // The committed list, written in step with setState so callbacks and
  // late responses always see the latest one (even before a re-render).
  const stateRef = useRef<ListState>(EMPTY);
  const commit = useCallback((next: ListState) => {
    stateRef.current = next;
    setState(next);
  }, []);
  // Bumped by every list-replacing fetch (first load, refresh) and by an
  // argument change; a response whose number is no longer current is
  // dropped, so an older project's page can never land on a newer one.
  const requestSeqRef = useRef(0);
  // Same for loadMore, so a dropped page cannot clear a newer one's flag.
  const loadMoreSeqRef = useRef(0);

  // Fetch `count` rows from the top and replace the list with them.
  const fetchWindow = useCallback(
    async (count: number) => {
      if (key === null) return;
      const seq = ++requestSeqRef.current;
      try {
        const response = await fetchDocs({ ...streamOpts, limit: count });
        if (seq !== requestSeqRef.current) return;
        commit({
          key,
          docs: response.docs,
          nextCursor: response.has_more ? response.next_cursor : null,
          error: null,
        });
      } catch (err) {
        if (seq !== requestSeqRef.current) return;
        // A background refresh failing keeps what is on screen; only a
        // list that never loaded for these arguments shows the error.
        if (stateRef.current.key === key) return;
        commit({
          key,
          docs: [],
          nextCursor: null,
          error: err instanceof ApiClientError ? err.message : 'Failed to load docs',
        });
      }
    },
    [key, streamOpts, commit],
  );

  // First page on mount and whenever the arguments change: drop the old
  // list (and any in-flight response for it) before fetching.
  useEffect(() => {
    requestSeqRef.current += 1;
    loadMoreSeqRef.current += 1;
    commit(EMPTY);
    setLoadingMore(false);
    void fetchWindow(pageSize);
  }, [fetchWindow, pageSize, commit]);

  const refresh = useCallback(async () => {
    if (key === null) return;
    const loaded = stateRef.current.key === key ? stateRef.current.docs.length : 0;
    await fetchWindow(Math.min(DOCS_MAX_PAGE_SIZE, Math.max(pageSize, loaded)));
  }, [fetchWindow, key, pageSize]);

  const loadMore = useCallback(() => {
    const cursor = stateRef.current.key === key ? stateRef.current.nextCursor : null;
    if (key === null || !cursor || loadingMore) return;
    const seq = requestSeqRef.current;
    const token = ++loadMoreSeqRef.current;
    setLoadingMore(true);
    void (async () => {
      try {
        const response = await fetchDocs({ ...streamOpts, limit: pageSize, cursor });
        // A refresh or argument change since the request started owns the
        // list now; appending onto it could skip or repeat rows.
        if (seq !== requestSeqRef.current) return;
        const prev = stateRef.current;
        if (prev.key !== key) return;
        // A refresh that was already in flight when this page was requested
        // shares our seq but may have landed meanwhile with a different
        // boundary; appending a page fetched from the old cursor onto it
        // could skip a row. The page is only valid for the cursor it extends.
        if (prev.nextCursor !== cursor) return;
        const seen = new Set(prev.docs.map((d) => d.id));
        commit({
          ...prev,
          docs: [...prev.docs, ...response.docs.filter((d) => !seen.has(d.id))],
          nextCursor: response.has_more ? response.next_cursor : null,
        });
      } catch {
        // Keep the list; the next click retries.
      } finally {
        if (token === loadMoreSeqRef.current) setLoadingMore(false);
      }
    })();
  }, [key, streamOpts, pageSize, loadingMore, commit]);

  // Realtime: any create / rename / delete / write / share change, trailing-
  // debounced so a burst costs one re-fetch (the first page on mount above
  // stays immediate).
  useEffect(() => {
    if (key === null) return;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const unsubscribe = persistentWebSocket.onGlobalEvent((event) => {
      if (event.type !== 'doc_list_changed') return;
      if (timer !== null) clearTimeout(timer);
      timer = setTimeout(() => {
        timer = null;
        void refresh();
      }, DOCS_REFRESH_DEBOUNCE_MS);
    });
    return () => {
      unsubscribe();
      if (timer !== null) clearTimeout(timer);
    };
  }, [key, refresh]);

  const current = key !== null && state.key === key ? state : EMPTY;
  return {
    docs: current.docs,
    loading: key !== null && state.key !== key,
    loadingMore: key !== null && loadingMore,
    error: current.error,
    hasMore: current.nextCursor !== null,
    loadMore,
    refresh,
  };
}
