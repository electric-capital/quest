import { act, renderHook, waitFor } from '@testing-library/react';
import { StrictMode } from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import {
  type AcceptDocNoteSuggestionOptions,
  DOC_NOTES_FEATURE,
  duplicateMemoryId,
  isStaleInstructionsError,
  staleInstructionsCurrent,
} from '../api/docsApi';
import { ApiClientError } from '../api/request';
import type {
  AcceptDocNoteSuggestionResponse,
  DismissDocNoteSuggestionResponse,
  DocMemory,
  DocNotes,
  DocNoteSuggestion,
  DocNoteSuggestionsListResponse,
} from '../api/types';
import { useDocNotes } from './useDocNotes';

type GlobalListener = (event: { type: string; [key: string]: unknown }) => void;

const mocks = vi.hoisted(() => ({
  fetchDocNotes: vi.fn<(docId: string) => Promise<DocNotes>>(),
  fetchDocNoteSuggestions: vi.fn<(docId: string) => Promise<DocNoteSuggestionsListResponse>>(),
  saveDocInstructions:
    vi.fn<(docId: string, instructions: string, expected: number) => Promise<DocNotes>>(),
  addDocMemory: vi.fn<(docId: string, content: string) => Promise<DocMemory>>(),
  updateDocMemory:
    vi.fn<(docId: string, memoryId: string, content: string) => Promise<DocMemory>>(),
  deleteDocMemory: vi.fn<(docId: string, memoryId: string) => Promise<{ success: boolean }>>(),
  acceptDocNoteSuggestion:
    vi.fn<(id: string, opts?: AcceptDocNoteSuggestionOptions) => Promise<AcceptDocNoteSuggestionResponse>>(),
  dismissDocNoteSuggestion: vi.fn<(id: string) => Promise<DismissDocNoteSuggestionResponse>>(),
  global: new Set<(event: { type: string; [key: string]: unknown }) => void>(),
  enabledFeatures: ['doc_notes'] as string[],
}));

vi.mock('../api/docsApi', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/docsApi')>()),
  fetchDocNotes: mocks.fetchDocNotes,
  fetchDocNoteSuggestions: mocks.fetchDocNoteSuggestions,
  saveDocInstructions: mocks.saveDocInstructions,
  addDocMemory: mocks.addDocMemory,
  updateDocMemory: mocks.updateDocMemory,
  deleteDocMemory: mocks.deleteDocMemory,
  acceptDocNoteSuggestion: mocks.acceptDocNoteSuggestion,
  dismissDocNoteSuggestion: mocks.dismissDocNoteSuggestion,
}));

vi.mock('../contexts/AuthContext', () => ({
  useAuth: () => ({ enabledFeatures: mocks.enabledFeatures }),
}));

vi.mock('../services/PersistentWebSocket', () => ({
  persistentWebSocket: {
    onGlobalEvent: (cb: GlobalListener) => {
      mocks.global.add(cb);
      return () => mocks.global.delete(cb);
    },
  },
}));

function memory(id: string, content = `Memory ${id}`): DocMemory {
  return {
    id,
    content,
    source_label: 'Added by you',
    created_at: '2026-10-01T00:00:00',
    updated_at: '2026-10-01T00:00:00',
  };
}

function notes(docId: string, overrides: Partial<DocNotes> = {}): DocNotes {
  return {
    doc_id: docId,
    instructions: `Instructions of ${docId}`,
    instructions_version: 1,
    instructions_updated_at: null,
    instructions_updated_by: null,
    memories: [memory('m1')],
    memories_version: 1,
    can_edit: true,
    is_owner: true,
    ...overrides,
  };
}

function suggestion(id: string, docId = 'd1'): DocNoteSuggestion {
  return {
    id,
    user_id: 1,
    doc_id: docId,
    conversation_id: 'c1',
    note_kind: 'memory',
    content: `Proposed ${id}`,
    reasoning: null,
    base_instructions_version: null,
    replaces_memory_id: null,
    status: 'pending',
    memory_id: null,
    created_at: '2026-10-01T00:00:00',
    resolved_at: null,
  };
}

function list(...rows: DocNoteSuggestion[]): DocNoteSuggestionsListResponse {
  return { suggestions: rows, pending_count: rows.length };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (err: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function emit(event: { type: string; [key: string]: unknown }) {
  for (const cb of mocks.global) cb(event);
}

/** The rejection of `promise`, which must reject with an ApiClientError. */
async function rejectionOf(promise: Promise<unknown>): Promise<ApiClientError> {
  try {
    await promise;
  } catch (err) {
    if (err instanceof ApiClientError) return err;
    throw err;
  }
  throw new Error('expected a rejection');
}

function renderNotes(docId: string | null | undefined) {
  return renderHook((id: string | null | undefined) => useDocNotes(id), {
    initialProps: docId,
  });
}

/** Render for `docId` and wait until its notes and suggestions are shown. */
async function renderLoaded(docId = 'd1') {
  const view = renderNotes(docId);
  await waitFor(() => expect(view.result.current.loading).toBe(false));
  // Await the suggestions response the hook is awaiting: the hook's own
  // continuation was registered first, so the list is committed by the time
  // this resumes.
  expect(mocks.fetchDocNoteSuggestions).toHaveBeenCalledTimes(1);
  await act(async () => {
    await mocks.fetchDocNoteSuggestions.mock.results[0]?.value;
  });
  expect(view.result.current.suggestionsError).toBeNull();
  return view;
}

const ids = (rows: { id: string }[]) => rows.map((row) => row.id);

describe('useDocNotes', () => {
  beforeEach(() => {
    for (const fn of [
      mocks.fetchDocNotes,
      mocks.fetchDocNoteSuggestions,
      mocks.saveDocInstructions,
      mocks.addDocMemory,
      mocks.updateDocMemory,
      mocks.deleteDocMemory,
      mocks.acceptDocNoteSuggestion,
      mocks.dismissDocNoteSuggestion,
    ]) {
      fn.mockReset();
    }
    mocks.fetchDocNotes.mockImplementation(async (docId) => notes(docId));
    mocks.fetchDocNoteSuggestions.mockImplementation(async () => list());
    mocks.global.clear();
    mocks.enabledFeatures = [DOC_NOTES_FEATURE];
  });

  it('does nothing while the gate is closed and loads once it opens', async () => {
    mocks.enabledFeatures = [];
    const { result, rerender } = renderNotes('d1');

    expect(result.current.enabled).toBe(false);
    expect(result.current.notes).toBeNull();
    expect(result.current.suggestions).toEqual([]);
    expect(result.current.loading).toBe(false);
    await act(async () => {
      await result.current.reload();
    });
    for (const call of [
      () => result.current.saveInstructions('text', 1),
      () => result.current.addMemory('text'),
      () => result.current.updateMemory('m1', 'text'),
      () => result.current.deleteMemory('m1'),
      () => result.current.acceptSuggestion('s1'),
      () => result.current.dismissSuggestion('s1'),
    ]) {
      expect((await rejectionOf(call())).errorCode).toBe('doc_notes_disabled');
    }
    for (const fn of [
      mocks.fetchDocNotes,
      mocks.fetchDocNoteSuggestions,
      mocks.saveDocInstructions,
      mocks.addDocMemory,
      mocks.updateDocMemory,
      mocks.deleteDocMemory,
      mocks.acceptDocNoteSuggestion,
      mocks.dismissDocNoteSuggestion,
    ]) {
      expect(fn).not.toHaveBeenCalled();
    }
    expect(mocks.global.size).toBe(0);

    mocks.enabledFeatures = [DOC_NOTES_FEATURE];
    rerender('d1');
    expect(result.current.enabled).toBe(true);
    expect(result.current.loading).toBe(true);
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(mocks.fetchDocNotes).toHaveBeenCalledWith('d1');
    expect(mocks.fetchDocNoteSuggestions).toHaveBeenCalledWith('d1');
    expect(result.current.notes?.doc_id).toBe('d1');
    expect(mocks.global.size).toBe(1);

    // Closing it again drops what was shown and the subscription.
    mocks.enabledFeatures = [];
    rerender('d1');
    expect(result.current.enabled).toBe(false);
    expect(result.current.notes).toBeNull();
    expect(mocks.global.size).toBe(0);
  });

  it.each([null, undefined, ''])('does nothing for an empty doc id (%j)', async (docId) => {
    const { result } = renderNotes(docId);
    expect(result.current.enabled).toBe(false);
    expect(result.current.loading).toBe(false);
    expect(result.current.notes).toBeNull();
    await act(async () => {
      await result.current.reload();
    });
    expect((await rejectionOf(result.current.addMemory('x'))).errorCode).toBe(
      'doc_notes_disabled',
    );
    expect(mocks.fetchDocNotes).not.toHaveBeenCalled();
    expect(mocks.fetchDocNoteSuggestions).not.toHaveBeenCalled();
    expect(mocks.addDocMemory).not.toHaveBeenCalled();
    expect(mocks.global.size).toBe(0);
  });

  it('loads the notes and the suggestions, loading until the notes land', async () => {
    const pending = deferred<DocNotes>();
    mocks.fetchDocNotes.mockReturnValueOnce(pending.promise);
    mocks.fetchDocNoteSuggestions.mockResolvedValueOnce(list(suggestion('s1'), suggestion('s2')));
    const { result } = renderNotes('d1');

    expect(result.current.enabled).toBe(true);
    expect(result.current.loading).toBe(true);
    expect(result.current.notes).toBeNull();
    await waitFor(() => expect(ids(result.current.suggestions)).toEqual(['s1', 's2']));
    // The suggestions landing first do not end the loading state.
    expect(result.current.loading).toBe(true);

    await act(async () => {
      pending.resolve(notes('d1'));
      await pending.promise;
    });
    expect(result.current.loading).toBe(false);
    expect(result.current.notes).toEqual(notes('d1'));
    expect(result.current.error).toBeNull();
    expect(result.current.notFound).toBe(false);
    expect(result.current.suggestionsError).toBeNull();
    expect(mocks.fetchDocNotes).toHaveBeenCalledTimes(1);
    expect(mocks.fetchDocNoteSuggestions).toHaveBeenCalledTimes(1);
  });

  it('reports notFound on a 404 and other failures as error', async () => {
    mocks.fetchDocNotes.mockRejectedValueOnce(new ApiClientError('x', 404, 'doc_not_found'));
    const { result } = renderNotes('missing');
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.notFound).toBe(true);
    expect(result.current.notes).toBeNull();
    expect(result.current.error).toBeNull();

    mocks.fetchDocNotes.mockRejectedValueOnce(new ApiClientError('off', 403, 'doc_notes_disabled'));
    const { result: failed } = renderNotes('d1');
    await waitFor(() => expect(failed.current.loading).toBe(false));
    expect(failed.current.notFound).toBe(false);
    expect(failed.current.error?.errorCode).toBe('doc_notes_disabled');

    mocks.fetchDocNotes.mockRejectedValueOnce(new TypeError('network down'));
    const { result: offline } = renderNotes('d2');
    await waitFor(() => expect(offline.current.loading).toBe(false));
    expect(offline.current.error).toBeInstanceOf(ApiClientError);
    expect(offline.current.error?.message).toBe('network down');
  });

  it('keeps the shown notes when a background refresh fails', async () => {
    const { result } = await renderLoaded('d1');
    mocks.fetchDocNotes.mockRejectedValueOnce(new ApiClientError('boom', 500));
    await act(async () => {
      await result.current.reload();
    });
    expect(result.current.notes?.doc_id).toBe('d1');
    expect(result.current.error).toBeNull();
  });

  it('hides the notes when a background refresh answers doc_notes_disabled', async () => {
    const { result } = await renderLoaded('d1');
    expect(result.current.notes).not.toBeNull();

    const disabled = new ApiClientError('off', 403, 'doc_notes_disabled');
    mocks.fetchDocNotes.mockRejectedValueOnce(disabled);
    await act(async () => {
      emit({ type: 'doc_notes_changed', doc_id: 'd1', instructions_version: 2, memories_version: 1 });
    });
    await waitFor(() => expect(result.current.notes).toBeNull());
    expect(result.current.error).toBe(disabled);
    expect(result.current.error?.errorCode).toBe('doc_notes_disabled');
    expect(result.current.notFound).toBe(false);
    expect(result.current.loading).toBe(false);
  });

  it('keeps the suggestions and reports suggestionsError when their load fails', async () => {
    mocks.fetchDocNoteSuggestions.mockResolvedValueOnce(list(suggestion('s1')));
    const { result } = await renderLoaded('d1');
    expect(ids(result.current.suggestions)).toEqual(['s1']);

    mocks.fetchDocNoteSuggestions.mockRejectedValueOnce(new ApiClientError('boom', 500));
    await act(async () => {
      emit({ type: 'doc_note_suggestions_changed', conversation_id: 'c1', pending_count: 2, total_pending: 2 });
    });
    await waitFor(() => expect(result.current.suggestionsError?.statusCode).toBe(500));
    expect(ids(result.current.suggestions)).toEqual(['s1']);
    expect(result.current.notes?.doc_id).toBe('d1');

    mocks.fetchDocNoteSuggestions.mockResolvedValueOnce(list(suggestion('s1'), suggestion('s2')));
    await act(async () => {
      await result.current.reload();
    });
    expect(result.current.suggestionsError).toBeNull();
    expect(ids(result.current.suggestions)).toEqual(['s1', 's2']);
  });

  it('a total_pending 0 clear also resets suggestionsError', async () => {
    mocks.fetchDocNoteSuggestions.mockResolvedValueOnce(list(suggestion('s1')));
    const { result } = await renderLoaded('d1');

    mocks.fetchDocNoteSuggestions.mockRejectedValueOnce(new ApiClientError('boom', 500));
    await act(async () => {
      await result.current.reload();
    });
    expect(result.current.suggestionsError?.statusCode).toBe(500);
    expect(ids(result.current.suggestions)).toEqual(['s1']);

    act(() =>
      emit({ type: 'doc_note_suggestions_changed', conversation_id: 'c1', pending_count: 0, total_pending: 0 }),
    );
    expect(result.current.suggestions).toEqual([]);
    expect(result.current.suggestionsError).toBeNull();
    expect(mocks.fetchDocNoteSuggestions).toHaveBeenCalledTimes(2);
  });

  it('follows doc_notes_changed for its own doc only, skipping versions already shown', async () => {
    const { result } = await renderLoaded('d1');

    act(() => emit({ type: 'doc_notes_changed', doc_id: 'other', instructions_version: 9, memories_version: 9 }));
    act(() => emit({ type: 'doc_notes_changed', doc_id: 'd1', instructions_version: 1, memories_version: 1 }));
    act(() => emit({ type: 'request_count_changed' }));
    expect(mocks.fetchDocNotes).toHaveBeenCalledTimes(1);

    mocks.fetchDocNotes.mockResolvedValueOnce(
      notes('d1', { memories: [memory('m2'), memory('m1')], memories_version: 2 }),
    );
    await act(async () => {
      emit({ type: 'doc_notes_changed', doc_id: 'd1', instructions_version: 1, memories_version: 2 });
    });
    await waitFor(() => expect(result.current.notes?.memories_version).toBe(2));
    expect(ids(result.current.notes?.memories ?? [])).toEqual(['m2', 'm1']);
    expect(mocks.fetchDocNotes).toHaveBeenCalledTimes(2);
    expect(mocks.fetchDocNoteSuggestions).toHaveBeenCalledTimes(1);
    expect(result.current.loading).toBe(false);
  });

  it('applies a refresh with equal versions (fresher rights)', async () => {
    const { result } = await renderLoaded('d1');
    mocks.fetchDocNotes.mockResolvedValueOnce(notes('d1', { can_edit: false, is_owner: false }));
    await act(async () => {
      await result.current.reload();
    });
    expect(result.current.notes?.can_edit).toBe(false);
    expect(result.current.notes?.is_owner).toBe(false);
  });

  it('re-fetches the suggestions on doc_note_suggestions_changed; total_pending 0 clears them', async () => {
    mocks.fetchDocNoteSuggestions.mockResolvedValueOnce(list(suggestion('s1')));
    const { result } = await renderLoaded('d1');
    expect(ids(result.current.suggestions)).toEqual(['s1']);

    mocks.fetchDocNoteSuggestions.mockResolvedValueOnce(list(suggestion('s1'), suggestion('s2')));
    await act(async () => {
      emit({ type: 'doc_note_suggestions_changed', conversation_id: 'other', pending_count: 1, total_pending: 2 });
    });
    await waitFor(() => expect(ids(result.current.suggestions)).toEqual(['s1', 's2']));
    expect(mocks.fetchDocNoteSuggestions).toHaveBeenCalledTimes(2);
    expect(mocks.fetchDocNotes).toHaveBeenCalledTimes(1);

    // A fetch in flight when the user's last pending row goes away is dropped.
    const late = deferred<DocNoteSuggestionsListResponse>();
    mocks.fetchDocNoteSuggestions.mockReturnValueOnce(late.promise);
    act(() =>
      emit({ type: 'doc_note_suggestions_changed', conversation_id: 'c1', pending_count: 1, total_pending: 1 }),
    );
    expect(mocks.fetchDocNoteSuggestions).toHaveBeenCalledTimes(3);
    act(() =>
      emit({ type: 'doc_note_suggestions_changed', conversation_id: 'c1', pending_count: 0, total_pending: 0 }),
    );
    expect(result.current.suggestions).toEqual([]);
    expect(mocks.fetchDocNoteSuggestions).toHaveBeenCalledTimes(3);

    await act(async () => {
      late.resolve(list(suggestion('s1')));
      await late.promise;
    });
    expect(result.current.suggestions).toEqual([]);
    expect(mocks.fetchDocNotes).toHaveBeenCalledTimes(1);
  });

  it('re-fetches both on doc_changed with a null updated_at for its own doc only', async () => {
    mocks.fetchDocNoteSuggestions.mockResolvedValueOnce(list(suggestion('s1')));
    const { result } = await renderLoaded('d1');
    expect(ids(result.current.suggestions)).toEqual(['s1']);

    act(() => emit({ type: 'doc_changed', doc_id: 'd1', updated_at: '2026-10-02T00:00:00' }));
    act(() => emit({ type: 'doc_changed', doc_id: 'other', updated_at: null }));
    act(() => emit({ type: 'doc_list_changed' }));
    expect(mocks.fetchDocNotes).toHaveBeenCalledTimes(1);
    expect(mocks.fetchDocNoteSuggestions).toHaveBeenCalledTimes(1);

    // A delete or a revoked share turns into notFound through the 404s,
    // and the suggestions 404 empties the list.
    mocks.fetchDocNotes.mockRejectedValueOnce(new ApiClientError('x', 404, 'doc_not_found'));
    mocks.fetchDocNoteSuggestions.mockRejectedValueOnce(
      new ApiClientError('x', 404, 'doc_not_found'),
    );
    await act(async () => {
      emit({ type: 'doc_changed', doc_id: 'd1', updated_at: null });
    });
    expect(mocks.fetchDocNotes).toHaveBeenCalledTimes(2);
    expect(mocks.fetchDocNoteSuggestions).toHaveBeenCalledTimes(2);
    await waitFor(() => expect(result.current.notFound).toBe(true));
    expect(result.current.notes).toBeNull();
    expect(result.current.loading).toBe(false);
    await waitFor(() => expect(result.current.suggestionsError?.statusCode).toBe(404));
    expect(result.current.suggestions).toEqual([]);
  });

  it('drops a previous doc after the id changes and follows the new doc', async () => {
    const firstNotes = deferred<DocNotes>();
    const firstSuggestions = deferred<DocNoteSuggestionsListResponse>();
    mocks.fetchDocNotes.mockReturnValueOnce(firstNotes.promise);
    mocks.fetchDocNoteSuggestions
      .mockReturnValueOnce(firstSuggestions.promise)
      .mockResolvedValueOnce(list(suggestion('s9', 'd2')));
    const { result, rerender, unmount } = renderNotes('d1');

    rerender('d2');
    expect(result.current.loading).toBe(true);
    await waitFor(() => expect(result.current.notes?.doc_id).toBe('d2'));
    await waitFor(() => expect(ids(result.current.suggestions)).toEqual(['s9']));

    await act(async () => {
      firstNotes.resolve(notes('d1', { instructions_version: 7 }));
      firstSuggestions.resolve(list(suggestion('s1')));
      await Promise.all([firstNotes.promise, firstSuggestions.promise]);
    });
    expect(result.current.notes?.doc_id).toBe('d2');
    expect(ids(result.current.suggestions)).toEqual(['s9']);

    act(() => emit({ type: 'doc_notes_changed', doc_id: 'd1', instructions_version: 9, memories_version: 9 }));
    act(() => emit({ type: 'doc_changed', doc_id: 'd1', updated_at: null }));
    expect(mocks.fetchDocNotes).toHaveBeenCalledTimes(2);

    mocks.fetchDocNotes.mockResolvedValueOnce(notes('d2', { instructions: 'new', instructions_version: 2 }));
    await act(async () => {
      emit({ type: 'doc_notes_changed', doc_id: 'd2', instructions_version: 2, memories_version: 1 });
    });
    await waitFor(() => expect(result.current.notes?.instructions).toBe('new'));
    expect(mocks.fetchDocNotes).toHaveBeenLastCalledWith('d2');
    expect(mocks.global.size).toBe(1);

    // Closing the panel clears it.
    rerender(null);
    expect(result.current.notes).toBeNull();
    expect(result.current.loading).toBe(false);
    expect(mocks.global.size).toBe(0);

    rerender('d2');
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(mocks.global.size).toBe(1);
    unmount();
    expect(mocks.global.size).toBe(0);
  });

  it('never lets an older fetch roll a newer save back', async () => {
    const { result } = await renderLoaded('d1');

    const stale = deferred<DocNotes>();
    mocks.fetchDocNotes.mockReturnValueOnce(stale.promise);
    act(() => emit({ type: 'doc_notes_changed', doc_id: 'd1', instructions_version: 2, memories_version: 1 }));

    mocks.saveDocInstructions.mockResolvedValueOnce(
      notes('d1', { instructions: 'saved', instructions_version: 3 }),
    );
    await act(async () => {
      await result.current.saveInstructions('saved', 1);
    });
    expect(result.current.notes?.instructions).toBe('saved');

    await act(async () => {
      stale.resolve(notes('d1', { instructions: 'older', instructions_version: 2 }));
      await stale.promise;
    });
    expect(result.current.notes?.instructions).toBe('saved');
    expect(result.current.notes?.instructions_version).toBe(3);

    // A memories counter behind the shown one is dropped too.
    mocks.fetchDocNotes.mockResolvedValueOnce(
      notes('d1', { instructions_version: 3, memories_version: 0, memories: [] }),
    );
    await act(async () => {
      await result.current.reload();
    });
    expect(ids(result.current.notes?.memories ?? [])).toEqual(['m1']);
  });

  it('saveInstructions sends the version it is given and applies the payload', async () => {
    const { result } = await renderLoaded('d1');
    const saved = notes('d1', { instructions: 'new text', instructions_version: 2 });
    mocks.saveDocInstructions.mockResolvedValueOnce(saved);

    let returned: DocNotes | undefined;
    await act(async () => {
      returned = await result.current.saveInstructions('new text', 1);
    });
    expect(mocks.saveDocInstructions).toHaveBeenCalledWith('d1', 'new text', 1);
    expect(returned).toEqual(saved);
    expect(result.current.notes).toEqual(saved);
    // A successful save is the truth: no re-fetch.
    expect(mocks.fetchDocNotes).toHaveBeenCalledTimes(1);

    // The caller's version is sent as is, even when a newer one is shown.
    mocks.saveDocInstructions.mockResolvedValueOnce(notes('d1', { instructions_version: 3 }));
    await act(async () => {
      await result.current.saveInstructions('again', 1);
    });
    expect(mocks.saveDocInstructions).toHaveBeenLastCalledWith('d1', 'again', 1);
  });

  it('saveInstructions rejects a stale version with the current text and re-fetches', async () => {
    const { result } = await renderLoaded('d1');
    const current = { instructions: 'theirs', instructions_version: 4 };
    const conflict = new ApiClientError('x', 409, 'stale_instructions', undefined, {
      error: 'stale_instructions',
      message: 'The instructions changed',
      current,
    });
    mocks.saveDocInstructions.mockRejectedValueOnce(conflict);
    mocks.fetchDocNotes.mockResolvedValueOnce(
      notes('d1', { instructions: 'theirs', instructions_version: 4 }),
    );

    let err: ApiClientError | undefined;
    await act(async () => {
      err = await rejectionOf(result.current.saveInstructions('mine', 1));
    });
    expect(err).toBe(conflict);
    expect(isStaleInstructionsError(err)).toBe(true);
    expect(staleInstructionsCurrent(err)).toEqual(current);
    expect(mocks.fetchDocNotes).toHaveBeenCalledTimes(2);
    await waitFor(() => expect(result.current.notes?.instructions).toBe('theirs'));
  });

  it('saveInstructions re-fetches the notes on a 404 and rethrows', async () => {
    const { result } = await renderLoaded('d1');
    const gone = new ApiClientError('gone', 404, 'doc_not_found');
    mocks.saveDocInstructions.mockRejectedValueOnce(gone);
    mocks.fetchDocNotes.mockRejectedValueOnce(new ApiClientError('gone', 404, 'doc_not_found'));

    let err: ApiClientError | undefined;
    await act(async () => {
      err = await rejectionOf(result.current.saveInstructions('mine', 1));
    });
    expect(err).toBe(gone);
    expect(mocks.fetchDocNotes).toHaveBeenCalledTimes(2);
    await waitFor(() => expect(result.current.notFound).toBe(true));
  });

  it('addMemory puts the memory at the top without a duplicate and re-fetches', async () => {
    const { result } = await renderLoaded('d1');

    const refetch = deferred<DocNotes>();
    mocks.fetchDocNotes.mockReturnValueOnce(refetch.promise);
    mocks.addDocMemory.mockResolvedValueOnce(memory('m2'));
    let added: DocMemory | undefined;
    await act(async () => {
      added = await result.current.addMemory('Memory m2');
    });
    expect(mocks.addDocMemory).toHaveBeenCalledWith('d1', 'Memory m2');
    expect(added).toEqual(memory('m2'));
    expect(ids(result.current.notes?.memories ?? [])).toEqual(['m2', 'm1']);
    expect(mocks.fetchDocNotes).toHaveBeenCalledTimes(2);
    await act(async () => {
      refetch.resolve(notes('d1', { memories: [memory('m2'), memory('m1')], memories_version: 2 }));
      await refetch.promise;
    });
    expect(result.current.notes?.memories_version).toBe(2);

    // A refresh that already lists the new memory: it is replaced in place.
    const pendingAdd = deferred<DocMemory>();
    mocks.addDocMemory.mockReturnValueOnce(pendingAdd.promise);
    let adding: Promise<DocMemory> | undefined;
    act(() => {
      adding = result.current.addMemory('Memory m3');
    });
    mocks.fetchDocNotes.mockResolvedValueOnce(
      notes('d1', {
        memories: [memory('m3', 'listed'), memory('m2'), memory('m1')],
        memories_version: 3,
      }),
    );
    await act(async () => {
      emit({ type: 'doc_notes_changed', doc_id: 'd1', instructions_version: 1, memories_version: 3 });
    });
    await waitFor(() => expect(result.current.notes?.memories_version).toBe(3));
    mocks.fetchDocNotes.mockReturnValueOnce(new Promise<DocNotes>(() => {}));
    await act(async () => {
      pendingAdd.resolve(memory('m3', 'returned'));
      await adding;
    });
    expect(result.current.notes?.memories.map((m) => [m.id, m.content])).toEqual([
      ['m3', 'returned'],
      ['m2', 'Memory m2'],
      ['m1', 'Memory m1'],
    ]);
  });

  it('a fetch started before a memory write cannot drop the new memory', async () => {
    const { result } = await renderLoaded('d1');

    // A reload's fetch is in flight with the pre-write list.
    const before = deferred<DocNotes>();
    mocks.fetchDocNotes.mockReturnValueOnce(before.promise);
    let reloading: Promise<void> | undefined;
    act(() => {
      reloading = result.current.reload();
    });
    expect(mocks.fetchDocNotes).toHaveBeenCalledTimes(2);

    mocks.addDocMemory.mockResolvedValueOnce(memory('m2'));
    mocks.fetchDocNotes.mockReturnValueOnce(new Promise<DocNotes>(() => {}));
    await act(async () => {
      await result.current.addMemory('Memory m2');
    });
    expect(ids(result.current.notes?.memories ?? [])).toEqual(['m2', 'm1']);
    expect(mocks.fetchDocNotes).toHaveBeenCalledTimes(3);

    // It lands with the same versions as the shown notes; the write's own
    // re-fetch superseded it, so it is dropped.
    await act(async () => {
      before.resolve(notes('d1'));
      await reloading;
    });
    expect(ids(result.current.notes?.memories ?? [])).toEqual(['m2', 'm1']);
  });

  it('updateMemory replaces in place and deleteMemory removes, each then re-fetching', async () => {
    mocks.fetchDocNotes.mockResolvedValueOnce(
      notes('d1', { memories: [memory('m2'), memory('m1')], memories_version: 2 }),
    );
    const { result } = await renderLoaded('d1');

    mocks.fetchDocNotes.mockReturnValueOnce(new Promise<DocNotes>(() => {}));
    mocks.updateDocMemory.mockResolvedValueOnce(memory('m1', 'edited'));
    let updated: DocMemory | undefined;
    await act(async () => {
      updated = await result.current.updateMemory('m1', 'edited');
    });
    expect(mocks.updateDocMemory).toHaveBeenCalledWith('d1', 'm1', 'edited');
    expect(updated?.content).toBe('edited');
    expect(result.current.notes?.memories.map((m) => m.content)).toEqual(['Memory m2', 'edited']);
    expect(mocks.fetchDocNotes).toHaveBeenCalledTimes(2);

    mocks.fetchDocNotes.mockReturnValueOnce(new Promise<DocNotes>(() => {}));
    mocks.deleteDocMemory.mockResolvedValueOnce({ success: true });
    await act(async () => {
      await result.current.deleteMemory('m2');
    });
    expect(mocks.deleteDocMemory).toHaveBeenCalledWith('d1', 'm2');
    expect(ids(result.current.notes?.memories ?? [])).toEqual(['m1']);
    expect(mocks.fetchDocNotes).toHaveBeenCalledTimes(3);
  });

  it('memory write errors reject unchanged; a 403 forbidden or doc_notes_disabled, 404 or 409 re-fetches the notes', async () => {
    const { result } = await renderLoaded('d1');
    const shown = result.current.notes;

    const duplicate = new ApiClientError('dup', 409, 'duplicate_memory', undefined, {
      detail: { error: 'duplicate_memory', message: 'dup', memory_id: 'm1' },
    });
    mocks.addDocMemory.mockRejectedValueOnce(duplicate);
    let err: ApiClientError | undefined;
    await act(async () => {
      err = await rejectionOf(result.current.addMemory('Memory m1'));
    });
    expect(err).toBe(duplicate);
    expect(duplicateMemoryId(err)).toBe('m1');
    expect(mocks.fetchDocNotes).toHaveBeenCalledTimes(2);

    const capped = new ApiClientError('cap', 409, 'memories_cap_reached');
    mocks.addDocMemory.mockRejectedValueOnce(capped);
    await act(async () => {
      err = await rejectionOf(result.current.addMemory('one more'));
    });
    expect(err).toBe(capped);
    expect(mocks.fetchDocNotes).toHaveBeenCalledTimes(3);

    const gone = new ApiClientError('gone', 404, 'memory_not_found');
    mocks.updateDocMemory.mockRejectedValueOnce(gone);
    await act(async () => {
      err = await rejectionOf(result.current.updateMemory('m9', 'x'));
    });
    expect(err).toBe(gone);
    expect(mocks.fetchDocNotes).toHaveBeenCalledTimes(4);

    // The viewer's rights moved (a write share downgraded).
    const forbidden = new ApiClientError('no', 403, 'forbidden');
    mocks.deleteDocMemory.mockRejectedValueOnce(forbidden);
    await act(async () => {
      err = await rejectionOf(result.current.deleteMemory('m1'));
    });
    expect(err).toBe(forbidden);
    expect(mocks.fetchDocNotes).toHaveBeenCalledTimes(5);

    const invalid = new ApiClientError('bad', 400, 'invalid_memory');
    mocks.addDocMemory.mockRejectedValueOnce(invalid);
    await act(async () => {
      err = await rejectionOf(result.current.addMemory(''));
    });
    expect(err).toBe(invalid);
    expect(mocks.fetchDocNotes).toHaveBeenCalledTimes(5);
    expect(result.current.notes).toEqual(shown);

    // The gate closed server-side: the re-fetch answers the same 403 and
    // hides the notes.
    const closed = new ApiClientError('off', 403, 'doc_notes_disabled');
    mocks.addDocMemory.mockRejectedValueOnce(closed);
    mocks.fetchDocNotes.mockRejectedValueOnce(closed);
    await act(async () => {
      err = await rejectionOf(result.current.addMemory('x'));
    });
    expect(err).toBe(closed);
    expect(mocks.fetchDocNotes).toHaveBeenCalledTimes(6);
    expect(result.current.notes).toBeNull();
    expect(result.current.error?.errorCode).toBe('doc_notes_disabled');
  });

  it('acceptSuggestion passes the options, removes the row and re-fetches both', async () => {
    mocks.fetchDocNoteSuggestions.mockResolvedValueOnce(list(suggestion('s1'), suggestion('s2')));
    const { result } = await renderLoaded('d1');
    expect(ids(result.current.suggestions)).toEqual(['s1', 's2']);

    const response: AcceptDocNoteSuggestionResponse = {
      suggestion: { ...suggestion('s1'), status: 'accepted', memory_id: 'm2' },
      memory: memory('m2', 'edited'),
      notes: { doc_id: 'd1', instructions_version: 1, memories_version: 2, has_instructions: true, memory_count: 2 },
    };
    mocks.acceptDocNoteSuggestion.mockResolvedValueOnce(response);
    mocks.fetchDocNotes.mockReturnValueOnce(new Promise<DocNotes>(() => {}));
    mocks.fetchDocNoteSuggestions.mockReturnValueOnce(
      new Promise<DocNoteSuggestionsListResponse>(() => {}),
    );
    const opts: AcceptDocNoteSuggestionOptions = { content: 'edited', expectedInstructionsVersion: 1 };
    let returned: AcceptDocNoteSuggestionResponse | undefined;
    await act(async () => {
      returned = await result.current.acceptSuggestion('s1', opts);
    });
    expect(mocks.acceptDocNoteSuggestion).toHaveBeenCalledWith('s1', {
      content: 'edited',
      expectedInstructionsVersion: 1,
    });
    expect(returned).toBe(response);
    expect(ids(result.current.suggestions)).toEqual(['s2']);
    expect(mocks.fetchDocNotes).toHaveBeenCalledTimes(2);
    expect(mocks.fetchDocNoteSuggestions).toHaveBeenCalledTimes(2);
  });

  it('dismissSuggestion removes the row and re-fetches the suggestions only', async () => {
    mocks.fetchDocNoteSuggestions.mockResolvedValueOnce(list(suggestion('s1'), suggestion('s2')));
    const { result } = await renderLoaded('d1');

    const row = { ...suggestion('s2'), status: 'dismissed' as const };
    mocks.dismissDocNoteSuggestion.mockResolvedValueOnce({ suggestion: row });
    mocks.fetchDocNoteSuggestions.mockReturnValueOnce(
      new Promise<DocNoteSuggestionsListResponse>(() => {}),
    );
    let returned: DocNoteSuggestion | undefined;
    await act(async () => {
      returned = await result.current.dismissSuggestion('s2');
    });
    expect(mocks.dismissDocNoteSuggestion).toHaveBeenCalledWith('s2');
    expect(returned).toEqual(row);
    expect(ids(result.current.suggestions)).toEqual(['s1']);
    expect(mocks.fetchDocNoteSuggestions).toHaveBeenCalledTimes(2);
    expect(mocks.fetchDocNotes).toHaveBeenCalledTimes(1);
  });

  it('a resolved suggestion leaves the list and the error is rethrown', async () => {
    mocks.fetchDocNoteSuggestions.mockResolvedValueOnce(list(suggestion('s1'), suggestion('s2')));
    const { result } = await renderLoaded('d1');

    const resolved = new ApiClientError('done', 409, 'suggestion_resolved');
    mocks.acceptDocNoteSuggestion.mockRejectedValueOnce(resolved);
    mocks.fetchDocNoteSuggestions.mockReturnValueOnce(
      new Promise<DocNoteSuggestionsListResponse>(() => {}),
    );
    let err: ApiClientError | undefined;
    await act(async () => {
      err = await rejectionOf(result.current.acceptSuggestion('s1'));
    });
    expect(err).toBe(resolved);
    expect(ids(result.current.suggestions)).toEqual(['s2']);
    expect(mocks.fetchDocNoteSuggestions).toHaveBeenCalledTimes(2);
    expect(mocks.fetchDocNotes).toHaveBeenCalledTimes(1);

    const missing = new ApiClientError('gone', 404, 'suggestion_not_found');
    mocks.dismissDocNoteSuggestion.mockRejectedValueOnce(missing);
    mocks.fetchDocNoteSuggestions.mockReturnValueOnce(
      new Promise<DocNoteSuggestionsListResponse>(() => {}),
    );
    await act(async () => {
      err = await rejectionOf(result.current.dismissSuggestion('s2'));
    });
    expect(err).toBe(missing);
    expect(result.current.suggestions).toEqual([]);
  });

  it('a stale accept keeps the row and re-fetches the notes and the suggestions', async () => {
    mocks.fetchDocNoteSuggestions.mockResolvedValueOnce(list(suggestion('s1')));
    const { result } = await renderLoaded('d1');

    const conflict = new ApiClientError('x', 409, 'stale_instructions', undefined, {
      error: 'stale_instructions',
      message: 'moved',
      current: { instructions: 'theirs', instructions_version: 5 },
    });
    mocks.acceptDocNoteSuggestion.mockRejectedValueOnce(conflict);
    mocks.fetchDocNotes.mockReturnValueOnce(new Promise<DocNotes>(() => {}));
    mocks.fetchDocNoteSuggestions.mockReturnValueOnce(
      new Promise<DocNoteSuggestionsListResponse>(() => {}),
    );
    let err: ApiClientError | undefined;
    await act(async () => {
      err = await rejectionOf(result.current.acceptSuggestion('s1', { expectedInstructionsVersion: 1 }));
    });
    expect(err).toBe(conflict);
    expect(staleInstructionsCurrent(err)?.instructions_version).toBe(5);
    expect(ids(result.current.suggestions)).toEqual(['s1']);
    expect(mocks.fetchDocNotes).toHaveBeenCalledTimes(2);
    expect(mocks.fetchDocNoteSuggestions).toHaveBeenCalledTimes(2);
  });

  it('a forbidden accept keeps the row and re-fetches the notes', async () => {
    mocks.fetchDocNoteSuggestions.mockResolvedValueOnce(list(suggestion('s1')));
    const { result } = await renderLoaded('d1');

    const forbidden = new ApiClientError('no', 403, 'forbidden');
    mocks.acceptDocNoteSuggestion.mockRejectedValueOnce(forbidden);
    mocks.fetchDocNotes.mockReturnValueOnce(new Promise<DocNotes>(() => {}));
    let err: ApiClientError | undefined;
    await act(async () => {
      err = await rejectionOf(result.current.acceptSuggestion('s1'));
    });
    expect(err).toBe(forbidden);
    expect(ids(result.current.suggestions)).toEqual(['s1']);
    expect(mocks.fetchDocNotes).toHaveBeenCalledTimes(2);
    expect(mocks.fetchDocNoteSuggestions).toHaveBeenCalledTimes(1);
  });

  it('a helper response for the previous doc does not touch the doc now open', async () => {
    mocks.fetchDocNoteSuggestions.mockResolvedValueOnce(list(suggestion('s1')));
    const { result, rerender } = await renderLoaded('d1');

    const pendingAdd = deferred<DocMemory>();
    mocks.addDocMemory.mockReturnValueOnce(pendingAdd.promise);
    const pendingAccept = deferred<AcceptDocNoteSuggestionResponse>();
    mocks.acceptDocNoteSuggestion.mockReturnValueOnce(pendingAccept.promise);
    let adding: Promise<DocMemory> | undefined;
    let accepting: Promise<AcceptDocNoteSuggestionResponse> | undefined;
    act(() => {
      adding = result.current.addMemory('for d1');
      accepting = result.current.acceptSuggestion('s1');
    });

    // The other doc happens to list a suggestion with the same id.
    mocks.fetchDocNoteSuggestions.mockResolvedValueOnce(list(suggestion('s1', 'd2')));
    rerender('d2');
    await waitFor(() => expect(result.current.notes?.doc_id).toBe('d2'));
    await waitFor(() => expect(ids(result.current.suggestions)).toEqual(['s1']));
    const shown = result.current.notes;

    await act(async () => {
      pendingAdd.resolve(memory('m2'));
      pendingAccept.resolve({
        suggestion: { ...suggestion('s1'), status: 'accepted' },
        memory: memory('m3'),
        notes: { doc_id: 'd1', instructions_version: 1, memories_version: 3, has_instructions: true, memory_count: 3 },
      });
      await Promise.all([adding, accepting]);
    });
    expect(result.current.notes).toBe(shown);
    expect(result.current.suggestions[0]?.doc_id).toBe('d2');
    // No background re-fetch was started for the previous doc.
    expect(mocks.fetchDocNotes.mock.calls.map(([id]) => id)).toEqual(['d1', 'd2']);
    expect(mocks.fetchDocNoteSuggestions.mock.calls.map(([id]) => id)).toEqual(['d1', 'd2']);
  });

  it('unsubscribes from the realtime layer on unmount', async () => {
    const { unmount } = await renderLoaded('d1');
    expect(mocks.global.size).toBe(1);
    unmount();
    expect(mocks.global.size).toBe(0);
  });

  it('holds exactly one listener under StrictMode', async () => {
    const { result, unmount } = renderHook(() => useDocNotes('d1'), { wrapper: StrictMode });
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(mocks.global.size).toBe(1);
    unmount();
    expect(mocks.global.size).toBe(0);
  });

  it('renders no more than the mount while disabled', async () => {
    mocks.enabledFeatures = [];
    let renders = 0;
    renderHook(() => {
      renders += 1;
      return useDocNotes('d1');
    });
    await act(async () => {});
    // The mount effect's reset commits the initial state, which bails out.
    expect(renders).toBe(1);

    let strictRenders = 0;
    renderHook(
      () => {
        strictRenders += 1;
        return useDocNotes('d1');
      },
      { wrapper: StrictMode },
    );
    await act(async () => {});
    // StrictMode's double render of the mount, nothing after it.
    expect(strictRenders).toBe(2);
    expect(mocks.fetchDocNotes).not.toHaveBeenCalled();
  });
});
