// The AutoProject unread hooks: a row's dot from its own seqs and the store,
// and the page acknowledging the AutoProject only while its newest update has
// been seen in a focused window (not on opening the page).
import { act, cleanup, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { unreadStore } from '../store/unreadStore';
import { useAutoProjectUnread, useMarkAutoProjectViewed } from './useAutoProjectUnread';

const mocks = vi.hoisted(() => ({
  markAutoProjectRead: vi.fn(),
}));

vi.mock('../api/autoprojectsApi', () => ({
  markAutoProjectRead: mocks.markAutoProjectRead,
}));

let focused = true;

beforeEach(() => {
  focused = true;
  vi.spyOn(document, 'hasFocus').mockImplementation(() => focused);
  vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('visible');
  mocks.markAutoProjectRead.mockReset().mockImplementation((_id: string, seq: number) =>
    Promise.resolve({ unread_seq: seq, read_seq: seq }),
  );
  unreadStore.reset();
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe('useAutoProjectUnread', () => {
  it.each([
    [{ unread_seq: 4, read_seq: 1 }, true],
    [{ unread_seq: 4, read_seq: 4 }, false],
    [{ unread_seq: 0, read_seq: 0 }, false],
  ])('reads the dot from the row %o', (seqs, unread) => {
    const { result } = renderHook(() => useAutoProjectUnread({ id: 'ap1', ...seqs }));
    expect(result.current).toBe(unread);
  });

  it('follows seqs merged into the store after render (a refetched list or detail)', () => {
    const { result } = renderHook(() => useAutoProjectUnread({ id: 'ap1', unread_seq: 0, read_seq: 0 }));
    expect(result.current).toBe(false);
    act(() => unreadStore.mergeAutoProject('ap1', 5, 2));
    expect(result.current).toBe(true);
    act(() => unreadStore.mergeAutoProject('ap1', 5, 5));
    expect(result.current).toBe(false);
  });
});

describe('useMarkAutoProjectViewed', () => {
  it('acknowledges nothing while the newest update has not been seen', () => {
    unreadStore.mergeAutoProject('ap1', 5, 2);
    renderHook(() => useMarkAutoProjectViewed('ap1', false));
    expect(mocks.markAutoProjectRead).not.toHaveBeenCalled();
  });

  it('acknowledges up to unread_seq once seen in a focused window, clearing the dot', async () => {
    unreadStore.mergeAutoProject('ap1', 5, 2);
    const dot = renderHook(() => useAutoProjectUnread({ id: 'ap1', unread_seq: 5, read_seq: 2 }));
    const { rerender } = renderHook(({ seen }) => useMarkAutoProjectViewed('ap1', seen), {
      initialProps: { seen: false },
    });
    rerender({ seen: true });
    expect(mocks.markAutoProjectRead).toHaveBeenCalledWith('ap1', 5);
    expect(dot.result.current).toBe(false);
    await act(async () => {});
    expect(dot.result.current).toBe(false);
  });

  it('waits for focus, and stops acknowledging once the page lets go', () => {
    focused = false;
    unreadStore.mergeAutoProject('ap1', 5, 2);
    const { unmount } = renderHook(() => useMarkAutoProjectViewed('ap1', true));
    expect(mocks.markAutoProjectRead).not.toHaveBeenCalled();

    focused = true;
    act(() => unreadStore.acknowledgeViewed());
    expect(mocks.markAutoProjectRead).toHaveBeenCalledTimes(1);

    unmount();
    act(() => unreadStore.mergeAutoProject('ap1', 9, 5));
    act(() => unreadStore.acknowledgeViewed());
    expect(mocks.markAutoProjectRead).toHaveBeenCalledTimes(1);
  });
});
