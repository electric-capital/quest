/**
 * Send-receipt behaviour of the WebSocketManager shim.
 *
 * Every tracked ``send_message`` must be confirmed by a
 * ``send_message_accepted`` receipt within the ack window; otherwise the
 * optimistic bubble flips into the failed state (Retry / Discard) instead of
 * hanging until a reload. The persistent socket is mocked so the tests drive
 * the exact envelopes the server would publish.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { Message, MessageContent } from '../api/types';

type Envelope = Record<string, unknown>;
type Handler = (event: Envelope) => void;

const mocks = vi.hoisted(() => {
  const handlers = new Map<string, Set<Handler>>();
  return {
    handlers,
    send: vi.fn<(payload: Envelope) => boolean>(() => true),
    notify: vi.fn<(title: string) => void>(),
    emit(conversationId: string, event: Envelope) {
      for (const h of Array.from(handlers.get(conversationId) ?? [])) h(event);
    },
    listenerCount(conversationId: string) {
      return handlers.get(conversationId)?.size ?? 0;
    },
  };
});

vi.mock('./PersistentWebSocket', () => ({
  persistentWebSocket: {
    send: (payload: Envelope) => mocks.send(payload),
    onConversationEvent: (conversationId: string, handler: Handler) => {
      let set = mocks.handlers.get(conversationId);
      if (!set) {
        set = new Set();
        mocks.handlers.set(conversationId, set);
      }
      set.add(handler);
      return () => {
        mocks.handlers.get(conversationId)?.delete(handler);
      };
    },
  },
}));

vi.mock('./desktopNotifications', () => ({
  requestNotificationPermission: () => {},
  sendDesktopNotification: (title: string) => mocks.notify(title),
}));

import { webSocketManager } from './WebSocketManager';
import { conversationStore } from '../store/conversationStore';

const ACK_TIMEOUT_MS = 15_000;

let counter = 0;

/** A conversation with one optimistic user bubble, as the hook creates it. */
function seed() {
  counter += 1;
  const id = `conv-${counter}`;
  const sendId = `send-${counter}`;
  const bubble: Message = {
    role: 'user',
    content: 'hello',
    timestamp: '2026-01-01T00:00:00Z',
    optimistic: true,
    client_send_id: sendId,
  };
  conversationStore.addMessage(id, bubble as MessageContent);
  return { id, sendId };
}

function send(id: string, sendId: string): void {
  webSocketManager.sendMessage(id, 'hello', 'model-x', undefined, undefined, undefined, undefined, undefined, sendId);
}

function bubble(id: string, sendId: string): Message | undefined {
  return conversationStore
    .getConversation(id)
    .messages.find((m) => (m as Message).client_send_id === sendId) as Message | undefined;
}

function lastPayload(): Envelope {
  return mocks.send.mock.calls[mocks.send.mock.calls.length - 1][0];
}

describe('webSocketManager send receipts', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    mocks.send.mockReset();
    mocks.send.mockImplementation(() => true);
    mocks.notify.mockReset();
    mocks.handlers.clear();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('writes the send_message envelope stamped with client_send_id and enters streaming', () => {
    const { id, sendId } = seed();
    send(id, sendId);

    expect(mocks.send).toHaveBeenCalledTimes(1);
    expect(lastPayload()).toMatchObject({
      op: 'send_message',
      conversation_id: id,
      message: 'hello',
      model: 'model-x',
      client_send_id: sendId,
    });
    expect(conversationStore.isStreaming(id)).toBe(true);
    expect(bubble(id, sendId)?.send_failed).toBeUndefined();
  });

  it('a timely receipt disarms the timeout and keeps the run streaming', () => {
    const { id, sendId } = seed();
    send(id, sendId);

    mocks.emit(id, { type: 'send_message_accepted', conversation_id: id, seq: 5, client_send_id: sendId });
    vi.advanceTimersByTime(ACK_TIMEOUT_MS + 1_000);

    expect(bubble(id, sendId)?.send_failed).toBeUndefined();
    expect(conversationStore.isStreaming(id)).toBe(true);
  });

  it('flags the bubble unconfirmed and leaves streaming when no receipt arrives in time', () => {
    const { id, sendId } = seed();
    send(id, sendId);

    vi.advanceTimersByTime(ACK_TIMEOUT_MS - 1);
    expect(bubble(id, sendId)?.send_failed).toBeUndefined();
    expect(conversationStore.isStreaming(id)).toBe(true);

    vi.advanceTimersByTime(1);
    expect(bubble(id, sendId)?.send_failed).toBe('unconfirmed');
    expect(conversationStore.isStreaming(id)).toBe(false);
    // The bubble itself stays so the user can Retry / Discard.
    expect(conversationStore.getConversation(id).messages).toHaveLength(1);
  });

  it('fails immediately with not_connected when the socket is closed', () => {
    const { id, sendId } = seed();
    mocks.send.mockImplementationOnce(() => false);
    send(id, sendId);

    expect(bubble(id, sendId)?.send_failed).toBe('not_connected');
    expect(conversationStore.isStreaming(id)).toBe(false);
  });

  it('a late receipt clears the failed state and re-enters streaming', () => {
    const { id, sendId } = seed();
    send(id, sendId);
    vi.advanceTimersByTime(ACK_TIMEOUT_MS);
    expect(bubble(id, sendId)?.send_failed).toBe('unconfirmed');

    mocks.emit(id, { type: 'send_message_accepted', conversation_id: id, seq: 5, client_send_id: sendId });

    expect(bubble(id, sendId)?.send_failed).toBeUndefined();
    expect(conversationStore.isStreaming(id)).toBe(true);

    // The run's events now render through a live buffer again.
    mocks.emit(id, { type: 'text_delta', conversation_id: id, content: 'partial' });
    expect(conversationStore.getConversation(id).partialResponse).toBe('partial');
  });

  it('retry re-sends the identical envelope; discard drops the bubble', () => {
    const { id, sendId } = seed();
    send(id, sendId);
    const original = lastPayload();
    vi.advanceTimersByTime(ACK_TIMEOUT_MS);

    webSocketManager.retryFailedSend(id, sendId);
    expect(mocks.send).toHaveBeenCalledTimes(2);
    expect(lastPayload()).toEqual(original);
    expect(bubble(id, sendId)?.send_failed).toBeUndefined();
    expect(conversationStore.isStreaming(id)).toBe(true);

    // Retry is a no-op while the retried send is still awaiting its receipt.
    webSocketManager.retryFailedSend(id, sendId);
    expect(mocks.send).toHaveBeenCalledTimes(2);

    vi.advanceTimersByTime(ACK_TIMEOUT_MS);
    expect(bubble(id, sendId)?.send_failed).toBe('unconfirmed');

    webSocketManager.discardFailedSend(id, sendId);
    expect(bubble(id, sendId)).toBeUndefined();
    expect(conversationStore.isStreaming(id)).toBe(false);
    expect(mocks.listenerCount(id)).toBe(0);
  });

  it('ignores the timeout once the row was reconciled through a catchup / tail fetch', () => {
    const { id, sendId } = seed();
    send(id, sendId);

    // The receipt itself was lost, but the persisted row arrived another way.
    const serverRow: Message = { role: 'user', content: 'hello', timestamp: '2026-01-01T00:00:01Z', seq: 3 };
    expect(conversationStore.reconcileOptimisticUserMessage(id, serverRow as MessageContent)).toBe(true);

    vi.advanceTimersByTime(ACK_TIMEOUT_MS);
    const [row] = conversationStore.getConversation(id).messages as Message[];
    expect(row.seq).toBe(3);
    expect(row.send_failed).toBeUndefined();
    expect(conversationStore.isStreaming(id)).toBe(true);
  });

  it('a server rejection rolls the in-flight bubble back without marking an interruption', () => {
    const { id, sendId } = seed();
    send(id, sendId);

    mocks.emit(id, { type: 'send_message_rejected', conversation_id: id, reason: 'busy' });

    expect(bubble(id, sendId)).toBeUndefined();
    expect(conversationStore.getConversation(id).messages).toEqual([]);
    expect(conversationStore.isStreaming(id)).toBe(false);
    expect(conversationStore.getConversation(id).error).toMatch(/already in progress/);
    vi.advanceTimersByTime(ACK_TIMEOUT_MS);
    expect(conversationStore.getConversation(id).messages).toEqual([]);
  });

  it('finishes the run on send_message_finished and notifies only on a clean completion', () => {
    const { id, sendId } = seed();
    send(id, sendId);
    mocks.emit(id, { type: 'send_message_accepted', conversation_id: id, seq: 5, client_send_id: sendId });

    mocks.emit(id, { type: 'text_delta', conversation_id: id, content: 'answer' });
    mocks.emit(id, { type: 'message_appended', conversation_id: id, seq: 6 });
    mocks.emit(id, { type: 'send_message_finished', conversation_id: id, error: true });

    expect(conversationStore.isStreaming(id)).toBe(false);
    expect(mocks.notify).not.toHaveBeenCalled();
    const messages = conversationStore.getConversation(id).messages as Message[];
    expect(messages.map((m) => [m.role, m.content, m.seq])).toEqual([
      ['user', 'hello', undefined],
      ['assistant', 'answer', 6],
    ]);
  });
});

describe('webSocketManager.syncRunState', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    mocks.send.mockReset();
    mocks.send.mockImplementation(() => true);
    mocks.handlers.clear();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('ignores run_active=false while a receipt is still outstanding, then drains on the next ack', () => {
    const { id, sendId } = seed();
    send(id, sendId);

    // The subscribe ack for this conversation is computed before the server
    // registered the run: must not kill the fresh send.
    mocks.emit(id, { type: 'subscribed', conversation_id: id, mode: 'up_to_date', run_active: false });
    expect(conversationStore.isStreaming(id)).toBe(true);

    mocks.emit(id, { type: 'send_message_accepted', conversation_id: id, seq: 5, client_send_id: sendId });

    // After the receipt, a reconnect ack saying the run is over (its
    // transient ``send_message_finished`` was lost in the gap) drains the
    // spinner without a synthetic "interrupted" marker.
    mocks.emit(id, { type: 'subscribed', conversation_id: id, mode: 'catchup', run_active: false, messages: [] });
    expect(conversationStore.isStreaming(id)).toBe(false);
    expect(conversationStore.getConversation(id).messages).toHaveLength(1);
  });

  it('enters the stop state for a run this tab did not start and leaves it on finish', () => {
    counter += 1;
    const id = `conv-${counter}`;
    expect(conversationStore.isStreaming(id)).toBe(false);

    webSocketManager.syncRunState(id, true);
    expect(conversationStore.isStreaming(id)).toBe(true);

    mocks.emit(id, { type: 'text_delta', conversation_id: id, content: 'from another tab' });
    expect(conversationStore.getConversation(id).partialResponse).toBe('from another tab');

    mocks.emit(id, { type: 'send_message_finished', conversation_id: id });
    expect(conversationStore.isStreaming(id)).toBe(false);
  });

  it('treats a missing run_active (older server) as a no-op', () => {
    counter += 1;
    const id = `conv-${counter}`;
    webSocketManager.syncRunState(id, undefined);
    expect(conversationStore.isStreaming(id)).toBe(false);
  });
});
