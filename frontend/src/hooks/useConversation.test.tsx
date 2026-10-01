/**
 * Catchup / resync handling in ``useConversation``.
 *
 * The hook owns the per-conversation subscription on the persistent socket
 * and turns the server's ``subscribed`` verdicts into store updates:
 * ``catchup`` applies the embedded rows, ``resync`` refetches the whole
 * conversation, ``message_appended`` tail-fetches past the last known seq.
 * The realtime singletons and the API client are mocked; the store is real.
 */
import { act, renderHook, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { ConversationDetail, Message, MessageContent } from '../api/types';
import { conversationStore } from '../store/conversationStore';
import { useConversation } from './useConversation';

type Envelope = Record<string, unknown>;
type Handler = (event: Envelope) => void;
type TailResponse = { messages: Array<Record<string, unknown>>; last_message_seq: number };

const mocks = vi.hoisted(() => {
  const handlers = new Map<string, Set<Handler>>();
  const lastSeq = new Map<string, number>();
  return {
    handlers,
    lastSeq,
    fetchConversation: vi.fn<(id: string) => Promise<ConversationDetail>>(),
    fetchConversationTail: vi.fn<(id: string, afterSeq: number) => Promise<TailResponse>>(),
    subscribe: vi.fn<(id: string) => void>(),
    syncRunState: vi.fn<(id: string, runActive: unknown) => void>(),
    attachToResumeStream: vi.fn<(id: string) => void>(),
    emit(conversationId: string, event: Envelope) {
      for (const h of Array.from(handlers.get(conversationId) ?? [])) h(event);
    },
  };
});

vi.mock('../api/client', () => ({
  fetchConversation: mocks.fetchConversation,
  fetchConversationTail: mocks.fetchConversationTail,
  fetchConversationLoadedSkills: vi.fn(),
  ApiClientError: class ApiClientError extends Error {},
}));

vi.mock('../services/PersistentWebSocket', () => ({
  persistentWebSocket: {
    subscribe: (id: string) => mocks.subscribe(id),
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
    onGlobalEvent: () => () => {},
    getLastSeq: (id: string) => mocks.lastSeq.get(id) ?? 0,
    setLastSeq: (id: string, seq: number) => {
      mocks.lastSeq.set(id, Math.max(seq, mocks.lastSeq.get(id) ?? 0));
    },
  },
}));

vi.mock('../services/WebSocketManager', () => ({
  webSocketManager: {
    syncRunState: (id: string, runActive: unknown) => mocks.syncRunState(id, runActive),
    attachToResumeStream: (id: string) => mocks.attachToResumeStream(id),
    sendMessage: vi.fn(),
    onConversationRenamed: () => () => {},
  },
}));

let counter = 0;

function message(role: Message['role'], content: string, seq: number): Message {
  return { role, content, timestamp: `2026-01-01T00:00:${String(seq).padStart(2, '0')}Z`, seq };
}

function detail(id: string, messages: Message[], overrides: Partial<ConversationDetail> = {}): ConversationDetail {
  const last = messages.length > 0 ? messages[messages.length - 1].seq ?? 0 : 0;
  return {
    id,
    user_id: 1,
    created_at: '2026-01-01T00:00:00Z',
    messages,
    last_message_seq: last,
    ...overrides,
  };
}

function rows(id: string): Array<[string, string, number | undefined]> {
  return (conversationStore.getConversation(id).messages as Message[]).map((m) => [m.role, m.content, m.seq]);
}

/** Mount the hook against a two-message conversation and wait for the load. */
async function mountLoaded() {
  counter += 1;
  const id = `conv-${counter}`;
  mocks.fetchConversation.mockResolvedValueOnce(
    detail(id, [message('user', 'hi', 1), message('assistant', 'hello', 2)]),
  );
  const rendered = renderHook(() => useConversation(id));
  await waitFor(() => expect(rendered.result.current.isLoaded).toBe(true));
  return { id, ...rendered };
}

describe('useConversation realtime reconciliation', () => {
  beforeEach(() => {
    mocks.fetchConversation.mockReset();
    mocks.fetchConversationTail.mockReset();
    mocks.subscribe.mockReset();
    mocks.syncRunState.mockReset();
    mocks.attachToResumeStream.mockReset();
    mocks.handlers.clear();
    mocks.lastSeq.clear();
  });

  it('loads the conversation, seeds the subscribe baseline and subscribes', async () => {
    const { id, result } = await mountLoaded();

    expect(mocks.fetchConversation).toHaveBeenCalledTimes(1);
    expect(mocks.subscribe).toHaveBeenCalledWith(id);
    expect(mocks.lastSeq.get(id)).toBe(2);
    expect(result.current.messages).toHaveLength(2);
    expect(rows(id)).toEqual([
      ['user', 'hi', 1],
      ['assistant', 'hello', 2],
    ]);
  });

  it('an up_to_date ack only reconciles the run state', async () => {
    const { id } = await mountLoaded();

    act(() => {
      mocks.emit(id, { type: 'subscribed', conversation_id: id, mode: 'up_to_date', current_seq: 2, run_active: true });
    });

    expect(mocks.syncRunState).toHaveBeenCalledWith(id, true);
    expect(mocks.fetchConversation).toHaveBeenCalledTimes(1);
    expect(mocks.fetchConversationTail).not.toHaveBeenCalled();
    expect(rows(id)).toHaveLength(2);
  });

  it('applies a catchup payload in seq order and reconciles the optimistic bubble', async () => {
    const { id } = await mountLoaded();

    // The user sent "again" just before the socket dropped: the bubble is
    // still optimistic when the reconnect catchup delivers its row.
    act(() => {
      conversationStore.addMessage(id, {
        role: 'user',
        content: 'again',
        timestamp: '2026-01-01T00:01:00Z',
        optimistic: true,
        client_send_id: 'send-1',
      } as MessageContent);
    });

    act(() => {
      mocks.emit(id, {
        type: 'subscribed',
        conversation_id: id,
        mode: 'catchup',
        current_seq: 4,
        run_active: false,
        // Out of order on purpose: insertion is by seq, not arrival order.
        messages: [message('assistant', 'late reply', 4), message('user', 'again', 3)],
      });
    });

    expect(mocks.syncRunState).toHaveBeenCalledWith(id, false);
    expect(mocks.fetchConversation).toHaveBeenCalledTimes(1);
    expect(rows(id)).toEqual([
      ['user', 'hi', 1],
      ['assistant', 'hello', 2],
      ['user', 'again', 3],
      ['assistant', 'late reply', 4],
    ]);
    const reconciled = conversationStore.getConversation(id).messages[2] as Message;
    expect(reconciled.optimistic).toBeUndefined();
    expect(reconciled.client_send_id).toBeUndefined();
  });

  it('a resync ack refetches the whole conversation and re-seeds the baseline', async () => {
    const { id } = await mountLoaded();
    mocks.fetchConversation.mockResolvedValueOnce(
      detail(id, [message('user', 'hi', 1), message('assistant', 'hello', 2), message('user', 'more', 10)]),
    );

    act(() => {
      mocks.emit(id, { type: 'subscribed', conversation_id: id, mode: 'resync', run_active: false });
    });

    await waitFor(() => expect(mocks.fetchConversation).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(rows(id)).toHaveLength(3));
    expect(rows(id)[2]).toEqual(['user', 'more', 10]);
    expect(mocks.lastSeq.get(id)).toBe(10);
  });

  it('a server-pushed resync event (bus overflow) takes the same recovery path', async () => {
    const { id } = await mountLoaded();
    mocks.fetchConversation.mockResolvedValueOnce(
      detail(id, [message('user', 'hi', 1), message('assistant', 'rewritten', 2)]),
    );

    act(() => {
      mocks.emit(id, { type: 'resync', conversation_id: id });
    });

    await waitFor(() => expect(mocks.fetchConversation).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(rows(id)[1]).toEqual(['assistant', 'rewritten', 2]));
  });

  it('message_appended tail-fetches past the last known seq and inserts the rows', async () => {
    const { id } = await mountLoaded();
    // The persistent socket records the event's seq before dispatching it.
    mocks.lastSeq.set(id, 3);
    mocks.fetchConversationTail.mockResolvedValueOnce({
      messages: [message('assistant', 'tail', 3) as unknown as Record<string, unknown>],
      last_message_seq: 3,
    });

    act(() => {
      mocks.emit(id, { type: 'message_appended', conversation_id: id, seq: 3 });
    });

    expect(mocks.fetchConversationTail).toHaveBeenCalledWith(id, 2);
    await waitFor(() => expect(rows(id)).toHaveLength(3));
    expect(rows(id)[2]).toEqual(['assistant', 'tail', 3]);

    // A duplicate delivery of the same row is deduped by seq.
    mocks.fetchConversationTail.mockResolvedValueOnce({
      messages: [message('assistant', 'tail', 3) as unknown as Record<string, unknown>],
      last_message_seq: 3,
    });
    act(() => {
      mocks.emit(id, { type: 'message_appended', conversation_id: id, seq: 3 });
    });
    await waitFor(() => expect(mocks.fetchConversationTail).toHaveBeenCalledTimes(2));
    expect(rows(id)).toHaveLength(3);
  });

  it('a headless resume_started enters the streaming state', async () => {
    const { id } = await mountLoaded();

    act(() => {
      mocks.emit(id, { type: 'resume_started', conversation_id: id });
    });

    expect(mocks.attachToResumeStream).toHaveBeenCalledWith(id);
  });
});
