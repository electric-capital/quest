import { act, renderHook, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { Conversation, ListConversationsResponse } from '../api/types';
import { CONVERSATIONS_PAGE_SIZE, useTopLevelConversations } from './useTopLevelConversations';

type Listener<T extends unknown[]> = (...args: T) => void;

const mocks = vi.hoisted(() => {
  const streamComplete = new Set<Listener<[]>>();
  const renamed = new Set<Listener<[string, string | null]>>();
  const global = new Set<Listener<[{ type: string; [key: string]: unknown }]>>();
  return {
    fetchConversations: vi.fn<(opts: Record<string, unknown>) => Promise<ListConversationsResponse>>(),
    streamComplete,
    renamed,
    global,
  };
});

vi.mock('../api/client', () => ({
  fetchConversations: mocks.fetchConversations,
  ApiClientError: class ApiClientError extends Error {},
}));

vi.mock('../services/WebSocketManager', () => ({
  webSocketManager: {
    onStreamComplete: (cb: Listener<[]>) => {
      mocks.streamComplete.add(cb);
      return () => mocks.streamComplete.delete(cb);
    },
    onConversationRenamed: (cb: Listener<[string, string | null]>) => {
      mocks.renamed.add(cb);
      return () => mocks.renamed.delete(cb);
    },
  },
}));

vi.mock('../services/PersistentWebSocket', () => ({
  persistentWebSocket: {
    onGlobalEvent: (cb: Listener<[{ type: string; [key: string]: unknown }]>) => {
      mocks.global.add(cb);
      return () => mocks.global.delete(cb);
    },
  },
}));

function conversation(id: string, overrides: Partial<Conversation> = {}): Conversation {
  return {
    id,
    title: `Title ${id}`,
    created_at: '2026-01-01T00:00:00Z',
    last_message_at: '2026-01-01T00:00:00Z',
    ...overrides,
  };
}

function page(ids: string[], nextCursor: string | null): ListConversationsResponse {
  return { conversations: ids.map((id) => conversation(id)), next_cursor: nextCursor, has_more: nextCursor !== null };
}

/** A promise the test resolves by hand, to inspect in-flight state. */
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((r) => {
    resolve = r;
  });
  return { promise, resolve };
}

const DEFAULT_QUERY = {
  includeArchived: false,
  excludeProjects: true,
  includeSlack: false,
  includeInference: false,
};

describe('useTopLevelConversations', () => {
  beforeEach(() => {
    mocks.fetchConversations.mockReset();
    mocks.streamComplete.clear();
    mocks.renamed.clear();
    mocks.global.clear();
  });

  it('loads the first page on mount with every filter off', async () => {
    mocks.fetchConversations.mockResolvedValueOnce(page(['a', 'b'], 'cursor-1'));
    const { result } = renderHook(() => useTopLevelConversations());

    expect(result.current.loading).toBe(true);
    await waitFor(() => expect(result.current.loading).toBe(false));

    expect(mocks.fetchConversations).toHaveBeenCalledTimes(1);
    expect(mocks.fetchConversations).toHaveBeenCalledWith({ ...DEFAULT_QUERY, limit: CONVERSATIONS_PAGE_SIZE });
    expect(result.current.conversations.map((c) => c.id)).toEqual(['a', 'b']);
    expect(result.current.hasMore).toBe(true);
  });

  it('drops rows that still belong to a project as a safety net', async () => {
    mocks.fetchConversations.mockResolvedValueOnce({
      conversations: [conversation('a'), conversation('p', { project_id: 'proj' })],
      next_cursor: null,
    });
    const { result } = renderHook(() => useTopLevelConversations());
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.conversations.map((c) => c.id)).toEqual(['a']);
    expect(result.current.hasMore).toBe(false);
  });

  it('refetches page one with the new filter when a toggle changes, hiding rows instantly', async () => {
    mocks.fetchConversations.mockResolvedValueOnce({
      conversations: [conversation('a')],
      next_cursor: null,
    });
    const { result } = renderHook(() => useTopLevelConversations());
    await waitFor(() => expect(result.current.loading).toBe(false));

    // Turning Slack on: the server is asked for Slack rows and they show up.
    mocks.fetchConversations.mockResolvedValueOnce({
      conversations: [conversation('a'), conversation('s', { origin: 'slack' })],
      next_cursor: null,
    });
    act(() => result.current.setFilter('slack', true));
    await waitFor(() => expect(result.current.conversations.map((c) => c.id)).toEqual(['a', 's']));
    expect(mocks.fetchConversations).toHaveBeenLastCalledWith({
      ...DEFAULT_QUERY,
      includeSlack: true,
      limit: CONVERSATIONS_PAGE_SIZE,
    });

    // Turning it off hides the Slack row before the refetch resolves.
    const pending = deferred<ListConversationsResponse>();
    mocks.fetchConversations.mockReturnValueOnce(pending.promise);
    act(() => result.current.setFilter('slack', false));
    expect(result.current.conversations.map((c) => c.id)).toEqual(['a']);
    expect(mocks.fetchConversations).toHaveBeenLastCalledWith({ ...DEFAULT_QUERY, limit: CONVERSATIONS_PAGE_SIZE });
    await act(async () => {
      pending.resolve({ conversations: [conversation('a')], next_cursor: null });
      await pending.promise;
    });
    await waitFor(() => expect(result.current.loading).toBe(false));
  });

  it('appends the next page deduped, then refreshes the whole loaded window silently', async () => {
    const firstIds = Array.from({ length: CONVERSATIONS_PAGE_SIZE }, (_, i) => `c${i}`);
    mocks.fetchConversations.mockResolvedValueOnce(page(firstIds, 'cursor-1'));
    const { result } = renderHook(() => useTopLevelConversations());
    await waitFor(() => expect(result.current.loading).toBe(false));

    // The second page overlaps on one row (bumped to the top since page one).
    mocks.fetchConversations.mockResolvedValueOnce(page([`c${CONVERSATIONS_PAGE_SIZE - 1}`, 'x1', 'x2'], null));
    await act(async () => {
      result.current.loadMore();
    });
    await waitFor(() => expect(result.current.loadingMore).toBe(false));

    expect(mocks.fetchConversations).toHaveBeenLastCalledWith({
      ...DEFAULT_QUERY,
      limit: CONVERSATIONS_PAGE_SIZE,
      cursor: 'cursor-1',
    });
    const loaded = CONVERSATIONS_PAGE_SIZE + 2;
    expect(result.current.conversations).toHaveLength(loaded);
    expect(result.current.hasMore).toBe(false);

    // A realtime list change re-fetches everything the user has paged
    // through (not just page one) without flashing the loading state.
    const pending = deferred<ListConversationsResponse>();
    mocks.fetchConversations.mockReturnValueOnce(pending.promise);
    act(() => {
      for (const cb of mocks.global) cb({ type: 'conversation_list_changed' });
    });
    expect(result.current.loading).toBe(false);
    expect(mocks.fetchConversations).toHaveBeenLastCalledWith({ ...DEFAULT_QUERY, limit: loaded });
    await act(async () => {
      pending.resolve(page([...firstIds, 'x1', 'x2'], null));
      await pending.promise;
    });
    expect(result.current.conversations).toHaveLength(loaded);
  });

  it('refreshes silently when a stream completes and ignores other global events', async () => {
    mocks.fetchConversations.mockResolvedValue(page(['a'], null));
    const { result } = renderHook(() => useTopLevelConversations());
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(mocks.fetchConversations).toHaveBeenCalledTimes(1);

    act(() => {
      for (const cb of mocks.global) cb({ type: 'request_count_changed' });
    });
    expect(mocks.fetchConversations).toHaveBeenCalledTimes(1);

    await act(async () => {
      for (const cb of mocks.streamComplete) cb();
    });
    expect(mocks.fetchConversations).toHaveBeenCalledTimes(2);
    expect(result.current.loading).toBe(false);
  });

  it('applies a rename event in place', async () => {
    mocks.fetchConversations.mockResolvedValueOnce(page(['a', 'b'], null));
    const { result } = renderHook(() => useTopLevelConversations());
    await waitFor(() => expect(result.current.loading).toBe(false));

    act(() => {
      for (const cb of mocks.renamed) cb('b', 'Renamed');
    });
    expect(result.current.conversations.find((c) => c.id === 'b')?.title).toBe('Renamed');
    expect(result.current.conversations.find((c) => c.id === 'a')?.title).toBe('Title a');
    // No refetch needed for a rename.
    expect(mocks.fetchConversations).toHaveBeenCalledTimes(1);
  });

  it('surfaces a load failure in the error slot and recovers on the next load', async () => {
    mocks.fetchConversations.mockRejectedValueOnce(new Error('boom'));
    const { result } = renderHook(() => useTopLevelConversations());
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.error).toBe('Failed to load conversations');

    mocks.fetchConversations.mockResolvedValueOnce(page(['a'], null));
    act(() => result.current.setFilter('archived', true));
    await waitFor(() => expect(result.current.conversations).toHaveLength(1));
    expect(result.current.error).toBeNull();
  });

  it('unsubscribes from the realtime layer on unmount', async () => {
    mocks.fetchConversations.mockResolvedValueOnce(page([], null));
    const { unmount, result } = renderHook(() => useTopLevelConversations());
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(mocks.global.size).toBe(1);
    unmount();
    expect(mocks.global.size).toBe(0);
    expect(mocks.streamComplete.size).toBe(0);
    expect(mocks.renamed.size).toBe(0);
  });
});
