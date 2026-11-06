// unreadStore: max-merging of the two unread seqs from racing sources, and
// the acknowledgement of the conversation on screen (focused window only,
// optimistic, retried after a failure).
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { unreadStore } from './unreadStore';

const mocks = vi.hoisted(() => ({
  markConversationRead: vi.fn(),
}));

vi.mock('../api/client', () => ({
  markConversationRead: mocks.markConversationRead,
}));

let focused = true;
let visible = true;

beforeEach(() => {
  focused = true;
  visible = true;
  vi.spyOn(document, 'hasFocus').mockImplementation(() => focused);
  vi.spyOn(document, 'visibilityState', 'get').mockImplementation(() =>
    visible ? 'visible' : 'hidden',
  );
  mocks.markConversationRead.mockReset();
  mocks.markConversationRead.mockImplementation((_id: string, seq: number) =>
    Promise.resolve({ unread_seq: seq, read_seq: seq }),
  );
  unreadStore.reset();
});

afterEach(() => {
  vi.restoreAllMocks();
});

/** Let the acknowledgement's promise chain settle. */
async function flush() {
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();
}

describe('unreadStore merging', () => {
  it('is unread while unread_seq is beyond read_seq', () => {
    expect(unreadStore.isUnread('c1')).toBe(false);
    unreadStore.merge('c1', 5, 2);
    expect(unreadStore.isUnread('c1')).toBe(true);
    unreadStore.merge('c1', 5, 5);
    expect(unreadStore.isUnread('c1')).toBe(false);
  });

  it('combines the caller row with what the store knows', () => {
    // Row only (nothing merged yet).
    expect(unreadStore.isUnread('c1', 4, 0)).toBe(true);
    // A stale row fetched before the read landed cannot bring the dot back.
    unreadStore.merge('c1', 4, 4);
    expect(unreadStore.isUnread('c1', 4, 0)).toBe(false);
    // A newer row than the store still shows.
    expect(unreadStore.isUnread('c1', 9, 4)).toBe(true);
  });

  it('never moves a seq backwards and ignores malformed values', () => {
    unreadStore.merge('c1', 8, 8);
    unreadStore.merge('c1', 3, undefined);
    unreadStore.merge('c1', 'x', null);
    expect(unreadStore.isUnread('c1')).toBe(false);
    unreadStore.merge('c1', 9, 0);
    expect(unreadStore.isUnread('c1')).toBe(true);
  });

  it('notifies listeners only on a change', () => {
    const listener = vi.fn();
    const unsubscribe = unreadStore.subscribe(listener);
    unreadStore.merge('c1', 5, 0);
    unreadStore.merge('c1', 5, 0);
    expect(listener).toHaveBeenCalledTimes(1);
    unsubscribe();
    unreadStore.merge('c1', 6, 0);
    expect(listener).toHaveBeenCalledTimes(1);
  });
});

describe('unreadStore acknowledging the viewed conversation', () => {
  it('acknowledges an unread conversation when it comes on screen', async () => {
    unreadStore.merge('c1', 5, 0);
    unreadStore.setViewed('c1');

    expect(mocks.markConversationRead).toHaveBeenCalledWith('c1', 5);
    expect(unreadStore.isUnread('c1')).toBe(false); // optimistic
    await flush();
    expect(unreadStore.isUnread('c1')).toBe(false);
  });

  it('never lets listeners see the viewed conversation as unread', () => {
    unreadStore.setViewed('c1');
    const seen: boolean[] = [];
    unreadStore.subscribe(() => seen.push(unreadStore.isUnread('c1')));

    unreadStore.merge('c1', 7, 0); // the run-end event

    expect(mocks.markConversationRead).toHaveBeenCalledWith('c1', 7);
    expect(seen.length).toBeGreaterThan(0);
    expect(seen).not.toContain(true);
  });

  it('leaves other conversations unread', () => {
    unreadStore.setViewed('c1');
    unreadStore.merge('c2', 7, 0);
    expect(mocks.markConversationRead).not.toHaveBeenCalled();
    expect(unreadStore.isUnread('c2')).toBe(true);
  });

  it('waits for focus when the window is unfocused', () => {
    focused = false;
    unreadStore.setViewed('c1');
    unreadStore.merge('c1', 7, 0);
    expect(mocks.markConversationRead).not.toHaveBeenCalled();
    expect(unreadStore.isUnread('c1')).toBe(true);

    focused = true;
    unreadStore.acknowledgeViewed();
    expect(mocks.markConversationRead).toHaveBeenCalledWith('c1', 7);
    expect(unreadStore.isUnread('c1')).toBe(false);
  });

  it('waits for visibility when the tab is hidden', () => {
    visible = false;
    unreadStore.setViewed('c1');
    unreadStore.merge('c1', 7, 0);
    expect(mocks.markConversationRead).not.toHaveBeenCalled();
  });

  it('stops acknowledging once the conversation leaves the screen', () => {
    unreadStore.setViewed('c1');
    unreadStore.clearViewed('c1');
    unreadStore.merge('c1', 7, 0);
    expect(mocks.markConversationRead).not.toHaveBeenCalled();
    expect(unreadStore.isUnread('c1')).toBe(true);
  });

  it('does not send a second request while one is in flight', () => {
    mocks.markConversationRead.mockReturnValue(new Promise(() => {}));
    unreadStore.merge('c1', 5, 0);
    unreadStore.setViewed('c1');
    unreadStore.acknowledgeViewed();
    unreadStore.acknowledgeViewed();
    expect(mocks.markConversationRead).toHaveBeenCalledTimes(1);
  });

  it('acknowledges again when a later run ends', async () => {
    unreadStore.merge('c1', 5, 0);
    unreadStore.setViewed('c1');
    await flush();

    unreadStore.merge('c1', 9, 5);

    expect(mocks.markConversationRead).toHaveBeenLastCalledWith('c1', 9);
    await flush();
    expect(unreadStore.isUnread('c1')).toBe(false);
  });

  it('brings the dot back after a failed request and retries on focus', async () => {
    mocks.markConversationRead.mockRejectedValueOnce(new Error('offline'));
    unreadStore.merge('c1', 5, 0);
    unreadStore.setViewed('c1');
    expect(unreadStore.isUnread('c1')).toBe(false);

    await flush();
    expect(unreadStore.isUnread('c1')).toBe(true);

    unreadStore.acknowledgeViewed();
    await flush();
    expect(mocks.markConversationRead).toHaveBeenCalledTimes(2);
    expect(unreadStore.isUnread('c1')).toBe(false);
  });
});
