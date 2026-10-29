/**
 * Data layer for a doc's Notes panel: the doc's instructions and memories
 * (`GET /docs/{id}/notes`) and the viewer's own pending note suggestions for
 * it (`GET /docs/{id}/note-suggestions`), with the edit and review calls,
 * which patch what is shown from their responses.
 *
 * Kept current by three realtime globals:
 * - `doc_notes_changed` for THIS doc re-fetches the notes unless the viewer
 *   already shows both of the event's versions. Other docs' events are
 *   ignored.
 * - `doc_note_suggestions_changed` re-fetches the suggestions. It carries a
 *   conversation id and counts but no doc id, so any change re-fetches; a
 *   `total_pending` of 0 (the user has no pending doc note anywhere) clears
 *   the list without a fetch.
 * - `doc_changed` for THIS doc with `updated_at: null` (a delete, a share
 *   added / changed / revoked) re-fetches both, since the viewer's access to
 *   the notes may have changed; that is how a lost doc turns into
 *   `notFound`. A body write (`updated_at` set) leaves the notes alone, and
 *   `doc_list_changed` is ignored.
 *
 * The notes carry two monotonic counters (`instructions_version`,
 * `memories_version`): a notes payload with either one lower than the shown
 * one is dropped, so an older fetch landing late never rolls a newer save
 * back.
 *
 * Gated by DOC_NOTES_FEATURE: with the gate closed or no doc id the hook
 * fetches nothing, subscribes to nothing, `reload` does nothing and the
 * action helpers reject with `doc_notes_disabled` without calling the
 * server. A notes load answering 403 `doc_notes_disabled` (the gate closed
 * server-side while the panel is open) hides the notes and sets `error`,
 * even when it is a background refresh, so the panel stops offering writes.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import {
  acceptDocNoteSuggestion,
  type AcceptDocNoteSuggestionOptions,
  addDocMemory,
  deleteDocMemory,
  dismissDocNoteSuggestion,
  DOC_NOTES_FEATURE,
  fetchDocNotes,
  fetchDocNoteSuggestions,
  saveDocInstructions,
  updateDocMemory,
} from '../api/docsApi';
import { ApiClientError } from '../api/request';
import type {
  AcceptDocNoteSuggestionResponse,
  DocMemory,
  DocNotes,
  DocNoteSuggestion,
} from '../api/types';
import { useAuth } from '../contexts/AuthContext';
import { persistentWebSocket } from '../services/PersistentWebSocket';

export interface DocNotesView {
  // Gate open AND a non-empty doc id; false means the hook does nothing.
  enabled: boolean;
  // Null while loading, disabled, not found or failed.
  notes: DocNotes | null;
  // The viewer's own pending suggestions for this doc, oldest first.
  suggestions: DocNoteSuggestion[];
  // True only until the first notes response for the current doc id lands
  // (never during a background refresh).
  loading: boolean;
  // A failed notes load other than 404 while no notes are shown, or a 403
  // `doc_notes_disabled` at any time (the notes are hidden then). Any other
  // background refresh failure keeps the shown notes and leaves this null.
  error: ApiClientError | null;
  // The notes GET answered 404 (the doc is gone or no longer visible).
  notFound: boolean;
  // The last suggestions load failed. The list is kept as it was, except
  // on a 404 (the doc is gone or hidden), which empties it.
  suggestionsError: ApiClientError | null;
  // Re-fetch the notes and the suggestions.
  reload: () => Promise<void>;
  // Replace the instructions. `expectedVersion` is sent as given (the
  // `instructions_version` the editor was opened with); a mismatch rejects
  // with stale_instructions (staleInstructionsCurrent).
  saveInstructions: (text: string, expectedVersion: number) => Promise<DocNotes>;
  addMemory: (content: string) => Promise<DocMemory>;
  updateMemory: (memoryId: string, content: string) => Promise<DocMemory>;
  deleteMemory: (memoryId: string) => Promise<void>;
  acceptSuggestion: (
    id: string,
    opts?: AcceptDocNoteSuggestionOptions,
  ) => Promise<AcceptDocNoteSuggestionResponse>;
  dismissSuggestion: (id: string) => Promise<DocNoteSuggestion>;
}

/**
 * What is shown, tagged with the doc id it belongs to (set as soon as that
 * doc starts loading).
 */
interface NotesState {
  id: string | null;
  notes: DocNotes | null;
  error: ApiClientError | null;
  notFound: boolean;
  suggestions: DocNoteSuggestion[];
  suggestionsError: ApiClientError | null;
}

const EMPTY: NotesState = {
  id: null,
  notes: null,
  error: null,
  notFound: false,
  suggestions: [],
  suggestionsError: null,
};

function disabledError(): ApiClientError {
  return new ApiClientError('Doc notes are not available', undefined, 'doc_notes_disabled');
}

function asApiError(err: unknown, fallback: string): ApiClientError {
  return err instanceof ApiClientError
    ? err
    : new ApiClientError(err instanceof Error ? err.message : fallback);
}

/**
 * A 403 `forbidden` or `doc_notes_disabled`, a 404 or a 409: the viewer's
 * rights or gate, the doc, the memory or a version moved on server-side, so
 * the shown notes are stale.
 */
function rightsOrStateMoved(err: unknown): boolean {
  return (
    err instanceof ApiClientError &&
    (err.errorCode === 'forbidden' ||
      err.errorCode === 'doc_notes_disabled' ||
      err.statusCode === 404 ||
      err.statusCode === 409)
  );
}

export function useDocNotes(docId: string | null | undefined): DocNotesView {
  const { enabledFeatures } = useAuth();
  // The doc the hook works for, or null while it is disabled.
  const activeId = enabledFeatures.includes(DOC_NOTES_FEATURE) && docId ? docId : null;

  const [state, setState] = useState<NotesState>(EMPTY);

  // The committed state, written in step with setState so callbacks and
  // late responses always see the latest one (even before a re-render).
  const stateRef = useRef<NotesState>(EMPTY);
  const commit = useCallback((next: NotesState) => {
    stateRef.current = next;
    setState(next);
  }, []);
  // Bumped by every fetch of their kind and by a doc change (the
  // suggestions one also by a cleared list); a response whose number is no
  // longer current is dropped, so a previous doc can never land on the one
  // now open, nor an older fetch overwrite a newer one.
  const notesSeqRef = useRef(0);
  const suggestionsSeqRef = useRef(0);

  // Show a notes payload for `forId` unless another doc is shown or it is
  // older than the shown notes. Equal counters apply: they may carry
  // fresher rights (`can_edit`, `is_owner`).
  const applyNotes = useCallback(
    (forId: string, notes: DocNotes) => {
      const prev = stateRef.current;
      if (prev.id !== forId) return;
      const shown = prev.notes;
      if (
        shown &&
        (notes.instructions_version < shown.instructions_version ||
          notes.memories_version < shown.memories_version)
      ) {
        return;
      }
      commit({ ...prev, notes, error: null, notFound: false });
    },
    [commit],
  );

  // Both fetches are no-ops unless the state is tagged with this doc, so a
  // helper finishing after the doc changed cannot start one for the old doc.
  const refreshNotes = useCallback(async () => {
    if (activeId === null || stateRef.current.id !== activeId) return;
    const seq = ++notesSeqRef.current;
    try {
      const notes = await fetchDocNotes(activeId);
      if (seq !== notesSeqRef.current) return;
      applyNotes(activeId, notes);
    } catch (err) {
      if (seq !== notesSeqRef.current) return;
      const prev = stateRef.current;
      if (err instanceof ApiClientError && err.statusCode === 404) {
        commit({ ...prev, notes: null, error: null, notFound: true });
        return;
      }
      if (err instanceof ApiClientError && err.errorCode === 'doc_notes_disabled') {
        commit({ ...prev, notes: null, error: err, notFound: false });
        return;
      }
      if (prev.notes) return;
      commit({ ...prev, error: asApiError(err, 'Failed to load notes'), notFound: false });
    }
  }, [activeId, applyNotes, commit]);

  const refreshSuggestions = useCallback(async () => {
    if (activeId === null || stateRef.current.id !== activeId) return;
    const seq = ++suggestionsSeqRef.current;
    try {
      const res = await fetchDocNoteSuggestions(activeId);
      if (seq !== suggestionsSeqRef.current) return;
      commit({ ...stateRef.current, suggestions: res.suggestions, suggestionsError: null });
    } catch (err) {
      if (seq !== suggestionsSeqRef.current) return;
      const prev = stateRef.current;
      const gone = err instanceof ApiClientError && err.statusCode === 404;
      commit({
        ...prev,
        suggestions: gone ? [] : prev.suggestions,
        suggestionsError: asApiError(err, 'Failed to load note suggestions'),
      });
    }
  }, [activeId, commit]);

  const reload = useCallback(async () => {
    await Promise.all([refreshNotes(), refreshSuggestions()]);
  }, [refreshNotes, refreshSuggestions]);

  // Load on mount and whenever the doc id or the gate changes, dropping the
  // previous doc (and any in-flight response for it) first.
  useEffect(() => {
    notesSeqRef.current += 1;
    suggestionsSeqRef.current += 1;
    commit(activeId === null ? EMPTY : { ...EMPTY, id: activeId });
    void refreshNotes();
    void refreshSuggestions();
  }, [activeId, commit, refreshNotes, refreshSuggestions]);

  useEffect(() => {
    if (activeId === null) return;
    return persistentWebSocket.onGlobalEvent((event) => {
      if (event.type === 'doc_notes_changed') {
        if (event.doc_id !== activeId) return;
        const prev = stateRef.current;
        const shown = prev.id === activeId ? prev.notes : null;
        if (
          shown &&
          shown.instructions_version === event.instructions_version &&
          shown.memories_version === event.memories_version
        ) {
          return;
        }
        void refreshNotes();
      } else if (event.type === 'doc_note_suggestions_changed') {
        if (event.total_pending !== 0) {
          void refreshSuggestions();
          return;
        }
        // Nothing pending anywhere: clear, and drop any fetch in flight.
        suggestionsSeqRef.current += 1;
        const prev = stateRef.current;
        if (prev.id === activeId) commit({ ...prev, suggestions: [], suggestionsError: null });
      } else if (
        event.type === 'doc_changed' &&
        event.doc_id === activeId &&
        event.updated_at === null
      ) {
        void refreshNotes();
        void refreshSuggestions();
      }
    });
  }, [activeId, commit, refreshNotes, refreshSuggestions]);

  // Rewrite the shown memories, if `forId` is still the doc shown.
  const patchMemories = useCallback(
    (forId: string, update: (memories: DocMemory[]) => DocMemory[]) => {
      const prev = stateRef.current;
      if (prev.id !== forId || !prev.notes) return;
      commit({ ...prev, notes: { ...prev.notes, memories: update(prev.notes.memories) } });
    },
    [commit],
  );

  // Await a notes write; an error for which rightsOrStateMoved holds
  // re-fetches the notes in the background before it is rethrown unchanged.
  const notesWrite = useCallback(
    async <T>(call: Promise<T>): Promise<T> => {
      try {
        return await call;
      } catch (err) {
        if (rightsOrStateMoved(err)) void refreshNotes();
        throw err;
      }
    },
    [refreshNotes],
  );

  const saveInstructions = useCallback(
    async (text: string, expectedVersion: number) => {
      if (activeId === null) throw disabledError();
      const notes = await notesWrite(saveDocInstructions(activeId, text, expectedVersion));
      applyNotes(activeId, notes);
      return notes;
    },
    [activeId, notesWrite, applyNotes],
  );

  // Each memory write patches the list from its response, then re-fetches
  // in the background; that fetch supersedes any older one in flight, which
  // would otherwise land with the pre-write list at an equal version.
  const addMemory = useCallback(
    async (content: string) => {
      if (activeId === null) throw disabledError();
      const memory = await notesWrite(addDocMemory(activeId, content));
      patchMemories(activeId, (list) =>
        list.some((m) => m.id === memory.id)
          ? list.map((m) => (m.id === memory.id ? memory : m))
          : [memory, ...list],
      );
      void refreshNotes();
      return memory;
    },
    [activeId, notesWrite, patchMemories, refreshNotes],
  );

  const updateMemory = useCallback(
    async (memoryId: string, content: string) => {
      if (activeId === null) throw disabledError();
      const memory = await notesWrite(updateDocMemory(activeId, memoryId, content));
      patchMemories(activeId, (list) => list.map((m) => (m.id === memory.id ? memory : m)));
      void refreshNotes();
      return memory;
    },
    [activeId, notesWrite, patchMemories, refreshNotes],
  );

  const deleteMemory = useCallback(
    async (memoryId: string) => {
      if (activeId === null) throw disabledError();
      await notesWrite(deleteDocMemory(activeId, memoryId));
      patchMemories(activeId, (list) => list.filter((m) => m.id !== memoryId));
      void refreshNotes();
    },
    [activeId, notesWrite, patchMemories, refreshNotes],
  );

  // Drop one suggestion row, if `forId` is still the doc shown.
  const removeSuggestion = useCallback(
    (forId: string, id: string) => {
      const prev = stateRef.current;
      if (prev.id !== forId || !prev.suggestions.some((s) => s.id === id)) return;
      commit({ ...prev, suggestions: prev.suggestions.filter((s) => s.id !== id) });
    },
    [commit],
  );

  // A failed accept / dismiss: an already resolved or vanished row leaves
  // the list; a stale instructions version re-reads the notes and the
  // rows' diffs; a `forbidden` (the viewer's rights moved) re-reads the
  // notes. The caller rethrows the error unchanged.
  const reviewFailed = useCallback(
    (forId: string, id: string, err: unknown) => {
      if (!(err instanceof ApiClientError)) return;
      if (err.errorCode === 'suggestion_resolved' || err.errorCode === 'suggestion_not_found') {
        removeSuggestion(forId, id);
        void refreshSuggestions();
      } else if (err.errorCode === 'stale_instructions') {
        void refreshNotes();
        void refreshSuggestions();
      } else if (err.errorCode === 'forbidden') {
        void refreshNotes();
      }
    },
    [removeSuggestion, refreshNotes, refreshSuggestions],
  );

  const acceptSuggestion = useCallback(
    async (id: string, opts?: AcceptDocNoteSuggestionOptions) => {
      if (activeId === null) throw disabledError();
      let res: AcceptDocNoteSuggestionResponse;
      try {
        res = await acceptDocNoteSuggestion(id, opts);
      } catch (err) {
        reviewFailed(activeId, id, err);
        throw err;
      }
      removeSuggestion(activeId, id);
      void refreshNotes();
      void refreshSuggestions();
      return res;
    },
    [activeId, reviewFailed, removeSuggestion, refreshNotes, refreshSuggestions],
  );

  const dismissSuggestion = useCallback(
    async (id: string) => {
      if (activeId === null) throw disabledError();
      let row: DocNoteSuggestion;
      try {
        row = (await dismissDocNoteSuggestion(id)).suggestion;
      } catch (err) {
        reviewFailed(activeId, id, err);
        throw err;
      }
      removeSuggestion(activeId, id);
      void refreshSuggestions();
      return row;
    },
    [activeId, reviewFailed, removeSuggestion, refreshSuggestions],
  );

  const current = activeId !== null && state.id === activeId ? state : EMPTY;
  return {
    enabled: activeId !== null,
    notes: current.notes,
    suggestions: current.suggestions,
    // Every notes response sets one of these three.
    loading: activeId !== null && !current.notes && !current.error && !current.notFound,
    error: current.error,
    notFound: current.notFound,
    suggestionsError: current.suggestionsError,
    reload,
    saveInstructions,
    addMemory,
    updateMemory,
    deleteMemory,
    acceptSuggestion,
    dismissSuggestion,
  };
}
