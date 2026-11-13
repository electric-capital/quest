// useOpenNotificationCount: the REST seed, the notifications_changed event
// replacing the count, an event without a numeric count re-fetching, a
// fetch never overwriting an event that landed after it started, and a
// failed fetch keeping the count.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, renderHook, waitFor } from '@testing-library/react';
import { useOpenNotificationCount } from './useOpenNotificationCount';

const mocks = vi.hoisted(() => ({
  fetchNotificationsCount: vi.fn(),
  globalHandlers: new Set<(event: Record<string, unknown>) => void>(),
}));

vi.mock('../api/client', () => ({
  fetchNotificationsCount: mocks.fetchNotificationsCount,
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

beforeEach(() => {
  mocks.fetchNotificationsCount.mockReset();
  mocks.globalHandlers.clear();
});

afterEach(cleanup);

describe('useOpenNotificationCount', () => {
  it('seeds from REST, then follows the event', async () => {
    mocks.fetchNotificationsCount.mockResolvedValue({ open_count: 2 });
    const { result } = renderHook(() => useOpenNotificationCount());
    expect(result.current).toBe(0);
    await waitFor(() => expect(result.current).toBe(2));

    emit({ type: 'notifications_changed', open_count: 1 });
    expect(result.current).toBe(1);
    // Other per-user globals leave it alone.
    emit({ type: 'memory_suggestions_changed', conversation_id: 'c', pending_count: 0, total_pending: 9 });
    expect(result.current).toBe(1);
    emit({ type: 'notifications_changed', open_count: 0 });
    expect(result.current).toBe(0);
  });

  it('re-fetches on an event without a numeric count', async () => {
    mocks.fetchNotificationsCount
      .mockResolvedValueOnce({ open_count: 1 })
      .mockResolvedValueOnce({ open_count: 4 });
    const { result } = renderHook(() => useOpenNotificationCount());
    await waitFor(() => expect(result.current).toBe(1));

    emit({ type: 'notifications_changed' });
    await waitFor(() => expect(result.current).toBe(4));
    expect(mocks.fetchNotificationsCount).toHaveBeenCalledTimes(2);
  });

  it('keeps an event that landed while the seed was in flight', async () => {
    const seed = deferred<{ open_count: number }>();
    mocks.fetchNotificationsCount.mockReturnValue(seed.promise);
    const { result } = renderHook(() => useOpenNotificationCount());

    emit({ type: 'notifications_changed', open_count: 3 });
    expect(result.current).toBe(3);
    await act(async () => {
      seed.resolve({ open_count: 7 });
      await seed.promise;
    });
    expect(result.current).toBe(3);
  });

  it('keeps the count when a fetch fails', async () => {
    mocks.fetchNotificationsCount
      .mockResolvedValueOnce({ open_count: 2 })
      .mockRejectedValueOnce(new Error('offline'));
    const { result } = renderHook(() => useOpenNotificationCount());
    await waitFor(() => expect(result.current).toBe(2));

    emit({ type: 'notifications_changed' });
    await waitFor(() => expect(mocks.fetchNotificationsCount).toHaveBeenCalledTimes(2));
    expect(result.current).toBe(2);
  });
});
