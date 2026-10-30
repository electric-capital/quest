// usePendingSuggestionCounts: the REST seed, each kind's event replacing its
// own count, an event without a numeric total re-fetching, a fetch never
// overwriting a kind that an event (or a newer fetch) set after it started,
// a failed re-fetch keeping the counts, and an unmount before the seed lands.
// Also the event-to-kind mapping the inbox filters its re-reads with.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, renderHook, waitFor } from '@testing-library/react';
import type { MemoryInboxCountsResponse } from '../api/types';
import {
  suggestionCountKind,
  totalPendingSuggestions,
  usePendingSuggestionCounts,
} from './usePendingSuggestionCounts';

const mocks = vi.hoisted(() => ({
  fetchMemoryInboxCounts: vi.fn(),
  globalHandlers: new Set<(event: Record<string, unknown>) => void>(),
}));

vi.mock('../api/client', () => ({
  fetchMemoryInboxCounts: mocks.fetchMemoryInboxCounts,
}));

vi.mock('../services/PersistentWebSocket', () => ({
  persistentWebSocket: {
    onGlobalEvent: (handler: (event: Record<string, unknown>) => void) => {
      mocks.globalHandlers.add(handler);
      return () => mocks.globalHandlers.delete(handler);
    },
  },
}));

function emit(event: Record<string, unknown>) {
  act(() => {
    mocks.globalHandlers.forEach((handler) => handler(event));
  });
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((res) => {
    resolve = res;
  });
  return { promise, resolve };
}

function counts(user: number, project: number, doc: number): MemoryInboxCountsResponse {
  return { user, project, doc, pending_count: user + project + doc };
}

beforeEach(() => {
  mocks.fetchMemoryInboxCounts.mockReset();
  mocks.globalHandlers.clear();
});

afterEach(cleanup);

describe('usePendingSuggestionCounts', () => {
  it('seeds from REST, then each event replaces its own kind', async () => {
    mocks.fetchMemoryInboxCounts.mockResolvedValue(counts(1, 2, 3));
    const { result } = renderHook(() => usePendingSuggestionCounts());
    expect(result.current).toEqual({ user: 0, project: 0, doc: 0 });
    await waitFor(() => expect(result.current).toEqual({ user: 1, project: 2, doc: 3 }));

    emit({ type: 'doc_note_suggestions_changed', conversation_id: 'c', pending_count: 0, total_pending: 5 });
    expect(result.current).toEqual({ user: 1, project: 2, doc: 5 });
    emit({ type: 'project_memory_suggestions_changed', conversation_id: 'c', pending_count: 0, total_pending: 0 });
    emit({ type: 'memory_suggestions_changed', conversation_id: 'c', pending_count: 0, total_pending: 4 });
    expect(result.current).toEqual({ user: 4, project: 0, doc: 5 });
    emit({ type: 'request_count_changed', counts: { open: 3 } });
    expect(result.current).toEqual({ user: 4, project: 0, doc: 5 });
    expect(mocks.fetchMemoryInboxCounts).toHaveBeenCalledTimes(1);
  });

  it('keeps a kind an event set while the seed was in flight', async () => {
    const seed = deferred<MemoryInboxCountsResponse>();
    mocks.fetchMemoryInboxCounts.mockImplementationOnce(() => seed.promise);
    const { result } = renderHook(() => usePendingSuggestionCounts());

    emit({ type: 'doc_note_suggestions_changed', conversation_id: 'c', pending_count: 1, total_pending: 7 });
    expect(result.current.doc).toBe(7);
    // The seed was read before the event: its doc count is older.
    await act(async () => {
      seed.resolve(counts(1, 2, 3));
    });
    expect(result.current).toEqual({ user: 1, project: 2, doc: 7 });
  });

  it('re-fetches on an event without a numeric total, keeping kinds set since', async () => {
    mocks.fetchMemoryInboxCounts.mockResolvedValueOnce(counts(1, 1, 1));
    const { result } = renderHook(() => usePendingSuggestionCounts());
    await waitFor(() => expect(result.current).toEqual({ user: 1, project: 1, doc: 1 }));

    const refetch = deferred<MemoryInboxCountsResponse>();
    mocks.fetchMemoryInboxCounts.mockImplementationOnce(() => refetch.promise);
    emit({ type: 'doc_note_suggestions_changed', conversation_id: 'c', pending_count: 2 });
    expect(mocks.fetchMemoryInboxCounts).toHaveBeenCalledTimes(2);
    expect(result.current.doc).toBe(1);

    emit({ type: 'memory_suggestions_changed', conversation_id: 'c', pending_count: 0, total_pending: 0 });
    await act(async () => {
      refetch.resolve(counts(6, 2, 2));
    });
    expect(result.current).toEqual({ user: 0, project: 2, doc: 2 });
  });

  it('lets a newer fetch win over an older one that lands later', async () => {
    mocks.fetchMemoryInboxCounts.mockResolvedValueOnce(counts(0, 0, 0));
    const { result } = renderHook(() => usePendingSuggestionCounts());
    await waitFor(() => expect(mocks.fetchMemoryInboxCounts).toHaveBeenCalledTimes(1));

    const older = deferred<MemoryInboxCountsResponse>();
    const newer = deferred<MemoryInboxCountsResponse>();
    mocks.fetchMemoryInboxCounts
      .mockImplementationOnce(() => older.promise)
      .mockImplementationOnce(() => newer.promise);
    emit({ type: 'doc_note_suggestions_changed', conversation_id: 'c' });
    emit({ type: 'project_memory_suggestions_changed', conversation_id: 'c' });
    await act(async () => {
      newer.resolve(counts(0, 3, 4));
    });
    await act(async () => {
      older.resolve(counts(0, 2, 2));
    });
    expect(result.current).toEqual({ user: 0, project: 3, doc: 4 });
  });

  it('keeps its last values when a re-fetch fails', async () => {
    mocks.fetchMemoryInboxCounts.mockResolvedValueOnce(counts(1, 2, 3));
    const { result } = renderHook(() => usePendingSuggestionCounts());
    await waitFor(() => expect(result.current).toEqual({ user: 1, project: 2, doc: 3 }));

    const refetch = deferred<MemoryInboxCountsResponse>();
    mocks.fetchMemoryInboxCounts.mockImplementationOnce(() => refetch.promise.then(() => {
      throw new Error('offline');
    }));
    emit({ type: 'doc_note_suggestions_changed', conversation_id: 'c', pending_count: 1 });
    expect(mocks.fetchMemoryInboxCounts).toHaveBeenCalledTimes(2);
    await act(async () => {
      refetch.resolve(counts(0, 0, 0));
    });
    expect(result.current).toEqual({ user: 1, project: 2, doc: 3 });
  });

  it('does nothing once unmounted before the seed lands', async () => {
    const seed = deferred<MemoryInboxCountsResponse>();
    mocks.fetchMemoryInboxCounts.mockImplementationOnce(() => seed.promise);
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {});
    try {
      const { result, unmount } = renderHook(() => usePendingSuggestionCounts());
      expect(mocks.globalHandlers.size).toBe(1);
      unmount();
      // The event subscription is gone with it.
      expect(mocks.globalHandlers.size).toBe(0);
      await act(async () => {
        seed.resolve(counts(1, 2, 3));
      });
      expect(result.current).toEqual({ user: 0, project: 0, doc: 0 });
      expect(consoleError).not.toHaveBeenCalled();
    } finally {
      consoleError.mockRestore();
    }
  });
});

describe('suggestionCountKind and totalPendingSuggestions', () => {
  it('maps each suggestion event to its kind, anything else to null', () => {
    expect(suggestionCountKind('memory_suggestions_changed')).toBe('user');
    expect(suggestionCountKind('project_memory_suggestions_changed')).toBe('project');
    expect(suggestionCountKind('doc_note_suggestions_changed')).toBe('doc');
    expect(suggestionCountKind('doc_notes_changed')).toBeNull();
    expect(suggestionCountKind(undefined)).toBeNull();
    expect(suggestionCountKind('toString')).toBeNull();
  });

  it('adds the three counts', () => {
    expect(totalPendingSuggestions({ user: 1, project: 2, doc: 3 })).toBe(6);
  });
});
