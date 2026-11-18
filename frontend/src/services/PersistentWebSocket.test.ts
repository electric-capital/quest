/**
 * Reconnect / catchup behaviour of the persistent WebSocket singleton, the
 * holder-counted conversation subscriptions, the reconnect hook for views fed
 * by per-user globals, and the global routing of the AutoProject events.
 *
 * The server decides between ``up_to_date`` / ``catchup`` / ``resync`` from
 * the ``last_seq`` the client sends on every ``subscribe``, so the client's
 * side of the contract is: remember the high-water seq per conversation,
 * persist it across reloads, and re-send it on every reconnect.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

type Envelope = Record<string, unknown>;

/** Scriptable stand-in for the browser WebSocket. */
class FakeWebSocket {
  static instances: FakeWebSocket[] = [];

  readonly url: string;
  readonly sent: Envelope[] = [];
  closed = false;
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  onclose: ((event: { code: number }) => void) | null = null;
  onerror: (() => void) | null = null;

  constructor(url: string) {
    this.url = url;
    FakeWebSocket.instances.push(this);
  }

  send(data: string): void {
    this.sent.push(JSON.parse(data) as Envelope);
  }

  close(): void {
    this.closed = true;
  }

  // -- test controls -------------------------------------------------------

  open(): void {
    this.onopen?.();
  }

  receive(event: Envelope): void {
    this.onmessage?.({ data: JSON.stringify(event) });
  }

  drop(code = 1006): void {
    this.onclose?.({ code });
  }

  sentOfType(op: string): Envelope[] {
    return this.sent.filter((e) => e.op === op);
  }
}

const STORAGE_KEY = 'quest_persistent_ws_last_seq';

/** Fresh singleton per test: the module builds it at import time. */
async function loadClient() {
  vi.resetModules();
  const mod = await import('./PersistentWebSocket');
  return mod.persistentWebSocket;
}

function latest(): FakeWebSocket {
  return FakeWebSocket.instances[FakeWebSocket.instances.length - 1];
}

describe('persistentWebSocket', () => {
  let client: Awaited<ReturnType<typeof loadClient>> | null = null;

  beforeEach(() => {
    vi.useFakeTimers();
    sessionStorage.clear();
    FakeWebSocket.instances = [];
    vi.stubGlobal('WebSocket', FakeWebSocket);
  });

  afterEach(() => {
    client?.disconnect();
    client = null;
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it('subscribes with last_seq 0 once the socket opens', async () => {
    client = await loadClient();
    client.connect();
    client.subscribe('c1');

    const ws = latest();
    expect(ws.sent).toEqual([]);

    ws.open();
    expect(ws.sentOfType('subscribe')).toEqual([
      { op: 'subscribe', conversation_id: 'c1', last_seq: 0 },
    ]);
  });

  it('routes conversation envelopes to conversation handlers and globals to global handlers', async () => {
    client = await loadClient();
    client.connect();
    latest().open();

    const conversationEvents: Envelope[] = [];
    const globalEvents: Envelope[] = [];
    client.onConversationEvent('c1', (e) => conversationEvents.push(e));
    client.onGlobalEvent((e) => globalEvents.push(e));

    latest().receive({ type: 'text_delta', conversation_id: 'c1', content: 'hi' });
    latest().receive({ type: 'conversation_list_changed', conversation_id: 'c1' });
    latest().receive({ type: 'request_count_changed', count: 2 });

    expect(conversationEvents.map((e) => e.type)).toEqual(['text_delta']);
    expect(globalEvents.map((e) => e.type)).toEqual(['conversation_list_changed', 'request_count_changed']);
  });

  it('keeps the suggestion-count globals off the conversation channel despite their conversation_id', async () => {
    client = await loadClient();
    client.connect();
    latest().open();

    const conversationEvents: Envelope[] = [];
    const globalEvents: Envelope[] = [];
    client.onConversationEvent('c1', (e) => conversationEvents.push(e));
    client.onGlobalEvent((e) => globalEvents.push(e));

    latest().receive({ type: 'doc_note_suggestions_changed', conversation_id: 'c1', pending_count: 2, total_pending: 3 });
    latest().receive({ type: 'project_memory_suggestions_changed', conversation_id: 'c1', pending_count: 1, total_pending: 1 });

    expect(conversationEvents).toEqual([]);
    expect(globalEvents).toEqual([
      { type: 'doc_note_suggestions_changed', conversation_id: 'c1', pending_count: 2, total_pending: 3 },
      { type: 'project_memory_suggestions_changed', conversation_id: 'c1', pending_count: 1, total_pending: 1 },
    ]);
  });

  it('reconnects after a drop and resubscribes with the recorded high-water seq', async () => {
    client = await loadClient();
    client.connect();
    client.subscribe('c1');
    const first = latest();
    first.open();

    first.receive({ type: 'message_appended', conversation_id: 'c1', seq: 7 });
    expect(client.getLastSeq('c1')).toBe(7);
    expect(JSON.parse(sessionStorage.getItem(STORAGE_KEY) ?? '{}')).toEqual({ c1: 7 });

    first.drop();
    expect(client.send({ op: 'ping' })).toBe(false);
    expect(FakeWebSocket.instances).toHaveLength(1);

    vi.advanceTimersByTime(1_000);
    expect(FakeWebSocket.instances).toHaveLength(2);
    const second = latest();
    second.open();

    expect(second.sentOfType('subscribe')).toEqual([
      { op: 'subscribe', conversation_id: 'c1', last_seq: 7 },
    ]);
  });

  it("uses the subscribed ack's current_seq as the catchup baseline and never lowers it", async () => {
    client = await loadClient();
    client.connect();
    client.subscribe('c1');
    latest().open();

    latest().receive({ type: 'subscribed', conversation_id: 'c1', mode: 'up_to_date', current_seq: 12 });
    expect(client.getLastSeq('c1')).toBe(12);

    // A stale durable event (e.g. replayed from an older catchup window)
    // must not move the baseline backwards.
    latest().receive({ type: 'message_appended', conversation_id: 'c1', seq: 3 });
    expect(client.getLastSeq('c1')).toBe(12);
  });

  it('restores last_seq from sessionStorage on a fresh load (tab reload)', async () => {
    sessionStorage.setItem(STORAGE_KEY, JSON.stringify({ c1: 9 }));
    client = await loadClient();

    expect(client.getLastSeq('c1')).toBe(9);

    client.connect();
    client.subscribe('c1');
    latest().open();
    expect(latest().sentOfType('subscribe')).toEqual([
      { op: 'subscribe', conversation_id: 'c1', last_seq: 9 },
    ]);
  });

  it('backs off exponentially between failed attempts and resets after a successful open', async () => {
    client = await loadClient();
    client.connect();
    latest().open();
    latest().drop();

    vi.advanceTimersByTime(999);
    expect(FakeWebSocket.instances).toHaveLength(1);
    vi.advanceTimersByTime(1);
    expect(FakeWebSocket.instances).toHaveLength(2);

    // Second consecutive failure: 2s.
    latest().drop();
    vi.advanceTimersByTime(1_999);
    expect(FakeWebSocket.instances).toHaveLength(2);
    vi.advanceTimersByTime(1);
    expect(FakeWebSocket.instances).toHaveLength(3);

    // A successful open resets the attempt counter back to the 1s base.
    latest().open();
    latest().drop();
    vi.advanceTimersByTime(1_000);
    expect(FakeWebSocket.instances).toHaveLength(4);
  });

  it('answers server pings and force-reconnects when no frame arrives within the watchdog deadline', async () => {
    vi.spyOn(console, 'warn').mockImplementation(() => {});
    client = await loadClient();
    client.connect();
    client.subscribe('c1');
    const first = latest();
    first.open();

    first.receive({ type: 'ping' });
    expect(first.sentOfType('pong')).toHaveLength(1);

    // Inbound traffic keeps the watchdog quiet...
    vi.advanceTimersByTime(50_000);
    first.receive({ type: 'ping' });
    vi.advanceTimersByTime(30_000);
    expect(FakeWebSocket.instances).toHaveLength(1);
    expect(first.closed).toBe(false);

    // ...but a silent half-open socket is torn down past the 60s deadline
    // and replaced through the normal backoff path, resubscribing.
    vi.advanceTimersByTime(40_000);
    expect(first.closed).toBe(true);
    expect(first.onclose).toBeNull();

    vi.advanceTimersByTime(1_000);
    expect(FakeWebSocket.instances).toHaveLength(2);
    latest().open();
    expect(latest().sentOfType('subscribe')).toEqual([
      { op: 'subscribe', conversation_id: 'c1', last_seq: 0 },
    ]);
  });

  it('ignores late events from a socket it already discarded', async () => {
    client = await loadClient();
    client.connect();
    const first = latest();
    first.open();
    const seen: Envelope[] = [];
    client.onConversationEvent('c1', (e) => seen.push(e));

    first.drop();
    vi.advanceTimersByTime(1_000);
    const second = latest();
    second.open();

    // The stale socket's handlers were detached on drop, so a late frame on
    // it cannot reach the handlers or clobber the live connection.
    first.receive({ type: 'text_delta', conversation_id: 'c1', content: 'ghost' });
    expect(seen).toEqual([]);
    expect(client.send({ op: 'ping' })).toBe(true);
    expect(second.sentOfType('ping')).toHaveLength(1);
  });

  // Several views can hold one conversation's channel at once (the AutoProject
  // activity line and the run panel's ChatPanel): the wire unsubscribe goes
  // out only when the last holder leaves.
  describe('holder-counted subscriptions', () => {
    /** Advance time with the server pinging, so the watchdog keeps the socket. */
    function advanceAlive(ws: FakeWebSocket, ms: number) {
      for (let t = 0; t < ms; t += 20_000) {
        vi.advanceTimersByTime(Math.min(20_000, ms - t));
        ws.receive({ type: 'ping' });
      }
    }

    it('keeps the subscription while another holder remains', async () => {
      client = await loadClient();
      client.connect();
      const ws = latest();
      ws.open();

      client.subscribe('c1');
      client.subscribe('c1');
      expect(ws.sentOfType('subscribe')).toHaveLength(2);

      client.unsubscribe('c1');
      expect(ws.sentOfType('unsubscribe')).toEqual([]);

      // Still held: the refresh tick re-subscribes...
      advanceAlive(ws, 180_000);
      expect(ws.sentOfType('subscribe')).toHaveLength(3);

      // ...and the last holder leaving sends the one wire unsubscribe.
      client.unsubscribe('c1');
      expect(ws.sentOfType('unsubscribe')).toEqual([{ op: 'unsubscribe', conversation_id: 'c1' }]);
      advanceAlive(ws, 180_000);
      expect(ws.sentOfType('subscribe')).toHaveLength(3);

      // An extra release is a no-op.
      client.unsubscribe('c1');
      expect(ws.sentOfType('unsubscribe')).toHaveLength(1);
    });

    it('re-subscribes a held conversation once on reconnect, however many holders', async () => {
      client = await loadClient();
      client.connect();
      client.subscribe('c1');
      client.subscribe('c1');
      client.subscribe('c2');
      client.unsubscribe('c2');
      latest().open();
      latest().drop();
      vi.advanceTimersByTime(1_000);
      latest().open();
      expect(latest().sentOfType('subscribe')).toEqual([
        { op: 'subscribe', conversation_id: 'c1', last_seq: 0 },
      ]);
    });

    it('resubscribe re-sends subscribe without taking a hold, and only for a held conversation', async () => {
      client = await loadClient();
      client.connect();
      const ws = latest();
      ws.open();

      client.resubscribe('c1');
      expect(ws.sentOfType('subscribe')).toEqual([]);

      client.subscribe('c1');
      client.resubscribe('c1');
      expect(ws.sentOfType('subscribe')).toHaveLength(2);

      // One unsubscribe releases the single hold.
      client.unsubscribe('c1');
      expect(ws.sentOfType('unsubscribe')).toHaveLength(1);
    });
  });

  describe('onReconnect', () => {
    it('fires on every reopen after the first, after the re-subscribes are sent', async () => {
      client = await loadClient();
      const order: string[] = [];
      const handler = vi.fn(() => {
        order.push(`reconnect after ${latest().sentOfType('subscribe').length} subscribe(s)`);
      });
      const off = client.onReconnect(handler);
      client.connect();
      client.subscribe('c1');
      latest().open();
      expect(handler).not.toHaveBeenCalled();

      latest().drop();
      vi.advanceTimersByTime(1_000);
      latest().open();
      expect(handler).toHaveBeenCalledTimes(1);
      expect(order).toEqual(['reconnect after 1 subscribe(s)']);

      off();
      latest().drop();
      vi.advanceTimersByTime(1_000);
      latest().open();
      expect(handler).toHaveBeenCalledTimes(1);
    });

    it('a throwing handler does not stop the others', async () => {
      vi.spyOn(console, 'error').mockImplementation(() => {});
      client = await loadClient();
      const ok = vi.fn();
      client.onReconnect(() => {
        throw new Error('boom');
      });
      client.onReconnect(ok);
      client.connect();
      latest().open();
      latest().drop();
      vi.advanceTimersByTime(1_000);
      latest().open();
      expect(ok).toHaveBeenCalledTimes(1);
    });
  });

  it('delivers the AutoProject globals to the global handlers', async () => {
    client = await loadClient();
    client.connect();
    latest().open();
    const conversationEvents: Envelope[] = [];
    const globalEvents: Envelope[] = [];
    client.onConversationEvent('c1', (e) => conversationEvents.push(e));
    client.onGlobalEvent((e) => globalEvents.push(e));

    const changed = {
      type: 'autoproject_changed',
      autoproject_id: 'ap1',
      event_seq: 4,
      brief_version: 2,
      active_run: { id: 'r1', seq: 1, status: 'running' },
    };
    latest().receive(changed);
    latest().receive({ type: 'autoproject_list_changed' });
    latest().receive({ type: 'file_list_changed', scope: 'autoproject', autoproject_id: 'ap1', conversation_id: 'c1' });

    expect(globalEvents).toEqual([
      changed,
      { type: 'autoproject_list_changed' },
      { type: 'file_list_changed', scope: 'autoproject', autoproject_id: 'ap1', conversation_id: 'c1' },
    ]);
    expect(conversationEvents).toEqual([]);
  });

  it('reports connected only while a socket is open', async () => {
    client = await loadClient();
    expect(client.connected).toBe(false);
    client.connect();
    expect(client.connected).toBe(false);
    latest().open();
    expect(client.connected).toBe(true);
    latest().drop();
    expect(client.connected).toBe(false);
  });
});
