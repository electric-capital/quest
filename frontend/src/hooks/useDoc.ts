/**
 * Data layer for the doc viewer: one doc's row + markdown body, kept current
 * by the realtime `doc_changed` global for THIS doc: it re-fetches unless
 * the viewer already shows that `updated_at`. The server sends
 * `updated_at: null` when the doc's access or existence changed (a delete,
 * a share added / changed / revoked, its project deleted), which always
 * re-fetches -- that is how a delete or a revoked share turns into
 * `notFound` through the follow-up 404. `doc_list_changed` is ignored here:
 * it is broadcast install-wide for writes to everyone-shared docs, and the
 * lists (useDocs) are what it is for.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { fetchDoc } from '../api/docsApi';
import { ApiClientError } from '../api/request';
import type { Doc, DocDetail } from '../api/types';
import { persistentWebSocket } from '../services/PersistentWebSocket';

export interface DocView {
  doc: DocDetail | null;
  // True only until the first response for the current docId lands (never
  // during a background refresh).
  loading: boolean;
  // A failed load other than 404, while no doc is shown. A background
  // refresh failing keeps the loaded doc and leaves this null.
  error: ApiClientError | null;
  // The doc does not exist or is not visible (404); `doc` and `error` are
  // null then.
  notFound: boolean;
  refresh: () => Promise<void>;
  // Merge a row returned by the rename / share endpoints (no `content`) into
  // the loaded doc, keeping its body.
  applyRow: (row: Doc) => void;
  // Merge a row WITH its body (a content save or a restore response) into
  // the loaded doc, replacing the body; the assets list is kept.
  applyContent: (row: Doc & { content: string }) => void;
}

/** What has been loaded, tagged with the doc id it was loaded for. */
interface DocState {
  id: string | null;
  doc: DocDetail | null;
  error: ApiClientError | null;
  notFound: boolean;
}

const EMPTY: DocState = { id: null, doc: null, error: null, notFound: false };

export function useDoc(docId: string | null): DocView {
  const [state, setState] = useState<DocState>(EMPTY);

  // The committed state, written in step with setState so callbacks and
  // late responses always see the latest one (even before a re-render).
  const stateRef = useRef<DocState>(EMPTY);
  const commit = useCallback((next: DocState) => {
    stateRef.current = next;
    setState(next);
  }, []);
  // Bumped by every fetch and by a docId change; a response whose number is
  // no longer current is dropped, so a previous doc can never land on the
  // one now open, nor an older fetch overwrite a newer one.
  const requestSeqRef = useRef(0);
  // Seq of the fetch in flight, or null.
  const pendingSeqRef = useRef<number | null>(null);

  const refresh = useCallback(async () => {
    if (docId === null) return;
    const seq = ++requestSeqRef.current;
    pendingSeqRef.current = seq;
    try {
      const detail = await fetchDoc(docId);
      if (seq !== requestSeqRef.current) return;
      commit({ id: docId, doc: detail, error: null, notFound: false });
    } catch (err) {
      if (seq !== requestSeqRef.current) return;
      if (err instanceof ApiClientError && err.statusCode === 404) {
        commit({ id: docId, doc: null, error: null, notFound: true });
        return;
      }
      const prev = stateRef.current;
      if (prev.id === docId && prev.doc) return;
      const error =
        err instanceof ApiClientError
          ? err
          : new ApiClientError(err instanceof Error ? err.message : 'Failed to load doc');
      commit({ id: docId, doc: null, error, notFound: false });
    } finally {
      if (pendingSeqRef.current === seq) pendingSeqRef.current = null;
    }
  }, [docId, commit]);

  // Load on mount and whenever the doc id changes, dropping the previous
  // doc (and any in-flight response for it) first.
  useEffect(() => {
    requestSeqRef.current += 1;
    pendingSeqRef.current = null;
    commit(EMPTY);
    void refresh();
  }, [refresh, commit]);

  const applyRow = useCallback(
    (row: Doc) => {
      const prev = stateRef.current;
      if (docId === null || row.id !== docId || prev.id !== docId || !prev.doc) return;
      // Timestamps are ISO strings of equal shape, so string order is time
      // order. A row older than what is shown (a rename response overtaken
      // by a newer refresh) must not roll the title/token back.
      if (row.updated_at < prev.doc.updated_at) return;
      commit({ ...prev, doc: { ...prev.doc, ...row } });
      // Rows carry no body. A NEWER `updated_at` (a rename response, or the
      // `current` row of a stale_update 409 after a write this tab missed)
      // may come with a body this tab has not seen -- size / asset count /
      // source can all coincide -- and an editor adopts the shown token, so
      // re-fetch rather than pair the new token with the old body. An equal
      // `updated_at` (a share change: access only) needs no body.
      const newer = row.updated_at > prev.doc.updated_at;
      // A fetch that started before this write would land with the old row;
      // re-fetch so the newest response wins instead.
      if (newer || pendingSeqRef.current !== null) void refresh();
    },
    [docId, commit, refresh],
  );

  const applyContent = useCallback(
    (row: Doc & { content: string }) => {
      const prev = stateRef.current;
      if (docId === null || row.id !== docId || prev.id !== docId || !prev.doc) return;
      if (row.updated_at < prev.doc.updated_at) return;
      // The row's fields and body replace the shown ones; `assets` and
      // `last_write_conversation` (not in the row) stay until a re-fetch.
      commit({ ...prev, doc: { ...prev.doc, ...row, content: row.content } });
      // The writer's conversation link depends on last_write_source, which a
      // UI save changes; an in-flight fetch could also land older data.
      if (row.last_write_source !== prev.doc.last_write_source || pendingSeqRef.current !== null) {
        void refresh();
      }
    },
    [docId, commit, refresh],
  );

  useEffect(() => {
    if (docId === null) return;
    return persistentWebSocket.onGlobalEvent((event) => {
      if (event.type === 'doc_changed' && event.doc_id === docId) {
        const prev = stateRef.current;
        const shown = prev.id === docId ? prev.doc?.updated_at : undefined;
        if (event.updated_at !== shown) void refresh();
      }
    });
  }, [docId, refresh]);

  const current = docId !== null && state.id === docId ? state : EMPTY;
  return {
    doc: current.doc,
    loading: docId !== null && state.id !== docId,
    error: current.error,
    notFound: current.notFound,
    refresh,
    applyRow,
    applyContent,
  };
}
