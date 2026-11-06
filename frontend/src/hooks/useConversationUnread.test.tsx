// The unread hooks: the realtime event and focus changes reaching the
// store, the main view reporting its conversation, and the row reading its
// dot state.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, renderHook } from '@testing-library/react';
import {
  useConversationUnread,
  useMarkConversationViewed,
  useUnreadTracking,
} from './useConversationUnread';
import { unreadStore } from '../store/unreadStore';

const mocks = vi.hoisted(() => ({
  markConversationRead: vi.fn(),
  globalHandlers: new Set<(event: Record<string, unknown>) => void>(),
}));

vi.mock('../api/client', () => ({
  markConversationRead: mocks.markConversationRead,
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

let focused = true;

beforeEach(() => {
  focused = true;
  vi.spyOn(document, 'hasFocus').mockImplementation(() => focused);
  vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('visible');
  mocks.globalHandlers.clear();
  mocks.markConversationRead.mockReset();
  mocks.markConversationRead.mockImplementation((_id: string, seq: number) =>
    Promise.resolve({ unread_seq: seq, read_seq: seq }),
  );
  unreadStore.reset();
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe('useConversationUnread', () => {
  it('reads the row and follows conversation_unread_changed', () => {
    renderHook(() => useUnreadTracking(true));
    const { result } = renderHook(() =>
      useConversationUnread({ id: 'c1', unread_seq: 0, read_seq: 0 }),
    );
    expect(result.current).toBe(false);

    emit({ type: 'conversation_unread_changed', conversation_id: 'c1', unread_seq: 4, read_seq: 0 });
    expect(result.current).toBe(true);

    emit({ type: 'conversation_unread_changed', conversation_id: 'c1', unread_seq: 4, read_seq: 4 });
    expect(result.current).toBe(false);
  });

  it('shows a row that arrives unread from the list', () => {
    const { result } = renderHook(() =>
      useConversationUnread({ id: 'c1', unread_seq: 4, read_seq: 1 }),
    );
    expect(result.current).toBe(true);
  });

  it('ignores other events', () => {
    renderHook(() => useUnreadTracking(true));
    const { result } = renderHook(() => useConversationUnread({ id: 'c1' }));
    emit({ type: 'conversation_list_changed', conversation_id: 'c1', unread_seq: 4, read_seq: 0 });
    expect(result.current).toBe(false);
  });
});

describe('useMarkConversationViewed', () => {
  it('acknowledges the loaded conversation, not one still loading', () => {
    unreadStore.merge('c1', 4, 0);
    const { rerender } = renderHook(
      ({ loaded }) => useMarkConversationViewed('c1', loaded),
      { initialProps: { loaded: false } },
    );
    expect(mocks.markConversationRead).not.toHaveBeenCalled();

    rerender({ loaded: true });
    expect(mocks.markConversationRead).toHaveBeenCalledWith('c1', 4);
  });

  it('acknowledges on window focus when the run ended while unfocused', () => {
    renderHook(() => useUnreadTracking(true));
    renderHook(() => useMarkConversationViewed('c1', true));
    const { result } = renderHook(() => useConversationUnread({ id: 'c1' }));

    focused = false;
    emit({ type: 'conversation_unread_changed', conversation_id: 'c1', unread_seq: 4, read_seq: 0 });
    expect(mocks.markConversationRead).not.toHaveBeenCalled();
    expect(result.current).toBe(true);

    focused = true;
    act(() => {
      window.dispatchEvent(new Event('focus'));
    });
    expect(mocks.markConversationRead).toHaveBeenCalledWith('c1', 4);
    expect(result.current).toBe(false);
  });

  it('does not acknowledge while impersonating, and resumes after', () => {
    const { rerender } = renderHook(
      ({ acknowledge }) => useUnreadTracking(acknowledge),
      { initialProps: { acknowledge: false } },
    );
    renderHook(() => useMarkConversationViewed('c1', true));
    const { result } = renderHook(() => useConversationUnread({ id: 'c1' }));

    emit({ type: 'conversation_unread_changed', conversation_id: 'c1', unread_seq: 4, read_seq: 0 });
    expect(mocks.markConversationRead).not.toHaveBeenCalled();
    expect(result.current).toBe(true);

    act(() => rerender({ acknowledge: true }));
    expect(mocks.markConversationRead).toHaveBeenCalledWith('c1', 4);
  });

  it('stops acknowledging after unmount', () => {
    renderHook(() => useUnreadTracking(true));
    const { unmount } = renderHook(() => useMarkConversationViewed('c1', true));
    unmount();

    emit({ type: 'conversation_unread_changed', conversation_id: 'c1', unread_seq: 4, read_seq: 0 });
    expect(mocks.markConversationRead).not.toHaveBeenCalled();
  });
});
