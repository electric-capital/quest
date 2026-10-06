/**
 * Data layer for the doc viewer: one doc's row + markdown body, kept current
 * by the realtime globals. `doc_changed` for this doc re-fetches unless the
 * viewer already shows that `updated_at`; `doc_list_changed` always
 * re-fetches, which is how a delete (it sends only that event) turns into
 * `notFound` through the follow-up 404.
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
  // Merge a row returned by the rename / mode endpoints (no `content`) into
  // the loaded doc, keeping its body.
  applyRow: (row: Doc) => void;
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
      // Rows carry no body. A changed size / asset count / write source
      // (e.g. the `current` row of a stale_update 409 after a model write
      // whose events this tab missed) means the shown content is stale too.
      const bodyChanged =
        row.content_size !== prev.doc.content_size
        || row.asset_count !== prev.doc.asset_count
        || row.last_write_source !== prev.doc.last_write_source;
      // A fetch that started before this write would land with the old row;
      // re-fetch so the newest response wins instead.
      if (bodyChanged || pendingSeqRef.current !== null) void refresh();
    },
    [docId, commit, refresh],
  );

  useEffect(() => {
    if (docId === null) return;
    return persistentWebSocket.onGlobalEvent((event) => {
      if (event.type === 'doc_list_changed') {
        void refresh();
      } else if (event.type === 'doc_changed' && event.doc_id === docId) {
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
  };
}
