/**
 * Persistent multiplexed WebSocket client.
 *
 * One singleton socket per browser session. Carries:
 *  - per-user globals (request_count_changed, conversation_list_changed,
 *    wait_handle_resolved)
 *  - per-conversation events (Phase 2: message_appended; Phase 3:
 *    text_delta, tool_started, sub_agent_*)
 *
 * Automatic reconnect with exponential backoff (1s -> 30s cap). On a 4401
 * close, probes /app/api/me before retrying so a logged-out session triggers
 * the unauth flow instead of looping. The per-conversation last_seq map is
 * persisted to sessionStorage so a tab reload reconnects with a usable
 * subscribe seq (Phase 2 wires this in).
 */
import { endpoints } from '../api/config';

const RECONNECT_BASE_MS = 1_000;
const RECONNECT_CAP_MS = 30_000;

const LAST_SEQ_STORAGE_KEY = 'quest_persistent_ws_last_seq';

const CLOSE_AUTH_FAILED = 4401;

// Liveness watchdog. The server pings every 25s; if a half-open connection
// silently drops server->client bytes, the OS won't notice because TCP keeps
// buffering our outbound writes. So we run our own deadline: if we haven't
// observed any inbound frame within ``WATCHDOG_DEADLINE_MS`` we force-close
// the socket, which fires ``onclose`` and triggers our normal reconnect /
// resubscribe / catchup flow.
//
// Additionally, we send our own client-originated ping every
// ``CLIENT_PING_INTERVAL_MS`` so the *server* observes liveness from us, and
// any reverse-proxy / NAT idle timeout on the path is reset by traffic in
// both directions. Picked at 25s (matches server cadence) so background-tab
// throttling still leaves room before the 60s deadline.
const CLIENT_PING_INTERVAL_MS = 25_000;
const WATCHDOG_DEADLINE_MS = 60_000;
const WATCHDOG_TICK_MS = 5_000;

// Per-conversation subscription refresh cadence. The server expires
// subscriptions after a 5 min TTL; we re-send ``subscribe`` every 3 min so
// a backgrounded tab (where ``setTimeout`` can be throttled to ~1 min
// granularity) still gets at least one refresh chance per TTL window.
const SUBSCRIPTION_REFRESH_INTERVAL_MS = 180_000;

export type PersistentEvent = Record<string, unknown> & { type?: string };

type GlobalHandler = (event: PersistentEvent) => void;
type ConversationHandler = (event: PersistentEvent) => void;

class PersistentWebSocketClient {
  private ws: WebSocket | null = null;
  private reconnectAttempt = 0;
  private reconnectTimer: number | null = null;
  private isOpen = false;
  private isClosing = false;

  private globalHandlers: Set<GlobalHandler> = new Set();
  private conversationHandlers: Map<string, Set<ConversationHandler>> = new Map();

  private subscribed: Set<string> = new Set();
  private lastSeqByConversation: Map<string, number> = new Map();
  private subscriptionRefreshTimers: Map<string, number> = new Map();

  // Liveness state. ``lastFrameAt`` is bumped on every inbound message;
  // ``watchdogTimer`` and ``pingTimer`` are scoped to a single open socket
  // and torn down on close.
  private lastFrameAt = 0;
  private pingTimer: number | null = null;
  private watchdogTimer: number | null = null;

  constructor() {
    this.lastSeqByConversation = this.loadLastSeqMap();
  }

  // ------------------------------------------------------------------
  // Connect / disconnect
  // ------------------------------------------------------------------

  connect(): void {
    if (this.ws !== null) return;
    this.isClosing = false;
    this.openSocket();
  }

  disconnect(): void {
    this.isClosing = true;
    if (this.reconnectTimer !== null) {
      window.clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    this.stopHeartbeat();
    this.clearAllRefreshTimers();
    if (this.ws) {
      this.discardSocket(this.ws);
      this.ws = null;
    }
    this.isOpen = false;
    this.subscribed.clear();
  }

  /**
   * Detach all handlers from a socket we no longer own, then close it.
   *
   * A discarded socket's ``onclose`` fires asynchronously; if the handlers
   * were left attached, a late close event from an OLD socket would clobber
   * ``this.ws`` / ``this.isOpen`` belonging to a NEWER socket and schedule a
   * spurious reconnect. That leaves the newer socket alive-but-orphaned:
   * still subscribed server-side, still dispatching into the singleton --
   * so every per-conversation event (e.g. ``text_delta``) is delivered
   * twice and streaming text renders doubled until the durable
   * ``message_appended`` path (deduped by seq) replaces it.
   */
  private discardSocket(ws: WebSocket): void {
    ws.onopen = null;
    ws.onmessage = null;
    ws.onclose = null;
    ws.onerror = null;
    try {
      ws.close();
    } catch {
      // ignore
    }
  }

  private openSocket(): void {
    const url = endpoints.persistentStream();
    let ws: WebSocket;
    try {
      ws = new WebSocket(url);
    } catch (err) {
      console.error('[PersistentWS] Failed to construct WebSocket:', err);
      this.scheduleReconnect();
      return;
    }
    this.ws = ws;

    // Every handler below guards on ``this.ws === ws``: a socket that has
    // been superseded (by disconnect/connect churn or a watchdog force-
    // reconnect racing its own async close event) must never mutate
    // singleton state, dispatch events, or schedule reconnects. Without the
    // guard, two live sockets can end up subscribed to the same
    // conversation and every transient event is processed twice.
    ws.onopen = () => {
      if (this.ws !== ws) {
        // Superseded while connecting; make sure it can't linger as a
        // second live connection.
        this.discardSocket(ws);
        return;
      }
      this.isOpen = true;
      this.reconnectAttempt = 0;
      this.lastFrameAt = Date.now();
      this.startHeartbeat();
      // Re-subscribe to all known conversations with their last-known seq
      // so the server can decide between up_to_date / catchup / resync.
      for (const conversationId of this.subscribed) {
        this.sendSubscribe(conversationId);
      }
    };

    ws.onmessage = (msg) => {
      if (this.ws !== ws) return;
      // Any inbound frame counts as liveness for the watchdog.
      this.lastFrameAt = Date.now();
      this.handleMessage(msg);
    };

    ws.onclose = (event) => {
      if (this.ws !== ws) return;
      this.isOpen = false;
      this.ws = null;
      this.stopHeartbeat();
      if (this.isClosing) return;
      if (event.code === CLOSE_AUTH_FAILED) {
        // Probe /me. If still authenticated (e.g. cookie was rotated mid-
        // socket and the next request will succeed), retry. Otherwise let
        // the existing app-level unauth flow take over.
        void this.handleAuthClose();
        return;
      }
      this.scheduleReconnect();
    };

    ws.onerror = () => {
      // onclose will handle the reconnect; nothing to do here.
    };
  }

  // ------------------------------------------------------------------
  // Heartbeat / watchdog
  // ------------------------------------------------------------------

  private startHeartbeat(): void {
    this.stopHeartbeat();
    // Client-originated ping keeps the path warm in both directions and
    // (combined with the server's own 25s pings) shortens the window during
    // which a half-open conn looks alive.
    this.pingTimer = window.setInterval(() => {
      // Send is best-effort; if the OS buffer has actually broken we'll
      // detect it via the watchdog deadline below.
      this.send({ op: 'ping' });
    }, CLIENT_PING_INTERVAL_MS);

    // Watchdog: if no inbound frame has been seen within the deadline,
    // assume the connection is half-open and force a reconnect. We tick
    // every WATCHDOG_TICK_MS instead of using a one-shot timer so the
    // deadline is checked even after `setTimeout` is throttled in a
    // background tab (the tick still fires, just less often).
    this.watchdogTimer = window.setInterval(() => {
      if (!this.isOpen || !this.ws) return;
      const idleMs = Date.now() - this.lastFrameAt;
      if (idleMs > WATCHDOG_DEADLINE_MS) {
        console.warn(
          `[PersistentWS] watchdog: no inbound frame for ${idleMs}ms; ` +
          'forcing reconnect',
        );
        this.forceReconnect();
      }
    }, WATCHDOG_TICK_MS);
  }

  private stopHeartbeat(): void {
    if (this.pingTimer !== null) {
      window.clearInterval(this.pingTimer);
      this.pingTimer = null;
    }
    if (this.watchdogTimer !== null) {
      window.clearInterval(this.watchdogTimer);
      this.watchdogTimer = null;
    }
  }

  private forceReconnect(): void {
    // Tear down the (probably-zombie) socket with its handlers detached --
    // its late `onclose` must not fire after a replacement socket exists.
    // On reconnect, the onopen path resubscribes every entry in
    // `this.subscribed` with its persisted last_seq so the server delivers
    // catchup/resync.
    this.stopHeartbeat();
    this.isOpen = false;
    const ws = this.ws;
    this.ws = null;
    if (ws) {
      this.discardSocket(ws);
    }
    this.scheduleReconnect();
  }

  private async handleAuthClose(): Promise<void> {
    try {
      const resp = await fetch(`${endpoints.me()}`, { credentials: 'include' });
      if (resp.ok) {
        this.scheduleReconnect();
        return;
      }
    } catch {
      // network error -- treat as needing reconnect
      this.scheduleReconnect();
      return;
    }
    // Otherwise stay closed; the app will redirect to sign-in via the
    // existing checkSession flow.
  }

  private scheduleReconnect(): void {
    if (this.reconnectTimer !== null) return;
    const delay = Math.min(
      RECONNECT_CAP_MS,
      RECONNECT_BASE_MS * Math.pow(2, this.reconnectAttempt),
    );
    this.reconnectAttempt += 1;
    this.reconnectTimer = window.setTimeout(() => {
      this.reconnectTimer = null;
      if (this.isClosing) return;
      this.openSocket();
    }, delay);
  }

  // ------------------------------------------------------------------
  // Message handling
  // ------------------------------------------------------------------

  private handleMessage(msg: MessageEvent): void {
    let event: PersistentEvent;
    try {
      event = JSON.parse(msg.data);
    } catch {
      return;
    }

    const type = event.type as string | undefined;

    // Heartbeat: respond with a pong so the server knows the socket is alive.
    if (type === 'ping') {
      this.send({ op: 'pong' });
      return;
    }

    // Pong response to our own client-originated ping (the watchdog already
    // bumped lastFrameAt in onmessage; nothing else to do).
    if (type === 'pong') {
      return;
    }

    // Per-conversation envelopes: route to conversation handlers.
    // Any envelope carrying a ``conversation_id`` is a per-conversation
    // event by definition; the explicit allow-list here is just to keep
    // accidental per-user globals (which do happen to mention a
    // conversation_id, e.g. ``conversation_list_changed``) on the global
    // channel. All other ``conversation_id``-stamped envelopes -- including
    // run-lifecycle signals like ``send_message_finished``,
    // ``send_message_rejected``, ``stop_acknowledged``, plus pass-through
    // types like ``conversation_updated`` and ``error`` -- must reach the
    // per-conversation handler so the streaming buffer can drain.
    const conversationId = event.conversation_id as string | undefined;
    const isPerUserGlobalWithConversationId = (
      type === 'conversation_list_changed'
      || type === 'wait_handle_resolved'
      || type === 'request_count_changed'
      || type === 'file_list_changed'
      || type === 'memory_suggestions_changed'
      || type === 'project_memory_suggestions_changed'
      || type === 'doc_note_suggestions_changed'
    );
    if (conversationId && !isPerUserGlobalWithConversationId) {
      // Track high-water seq from durable events so a reconnect can issue
      // a smart catchup instead of a full resync.
      if (type === 'message_appended') {
        const seq = event.seq as number | undefined;
        if (typeof seq === 'number') {
          this.recordLastSeq(conversationId, seq);
        }
      }
      if (type === 'subscribed') {
        const seq = event.current_seq as number | undefined;
        if (typeof seq === 'number') {
          this.recordLastSeq(conversationId, seq);
        }
      }

      const handlers = this.conversationHandlers.get(conversationId);
      if (handlers) {
        handlers.forEach((h) => {
          try {
            h(event);
          } catch (err) {
            console.error('[PersistentWS] conversation handler error:', err);
          }
        });
      }
      return;
    }

    // Per-user globals: dispatch to all global handlers.
    this.globalHandlers.forEach((h) => {
      try {
        h(event);
      } catch (err) {
        console.error('[PersistentWS] global handler error:', err);
      }
    });
  }

  // ------------------------------------------------------------------
  // Public API
  // ------------------------------------------------------------------

  send(payload: Record<string, unknown>): boolean {
    if (!this.ws || !this.isOpen) return false;
    try {
      this.ws.send(JSON.stringify(payload));
      return true;
    } catch (err) {
      console.warn('[PersistentWS] send failed:', err);
      return false;
    }
  }

  subscribe(conversationId: string): void {
    this.subscribed.add(conversationId);
    if (this.isOpen) {
      this.sendSubscribe(conversationId);
    }
    this.ensureRefreshTimer(conversationId);
  }

  unsubscribe(conversationId: string): void {
    this.subscribed.delete(conversationId);
    this.clearRefreshTimer(conversationId);
    if (this.isOpen) {
      this.send({ op: 'unsubscribe', conversation_id: conversationId });
    }
  }

  private ensureRefreshTimer(conversationId: string): void {
    const existing = this.subscriptionRefreshTimers.get(conversationId);
    if (existing !== undefined) {
      window.clearInterval(existing);
    }
    // Timer runs unconditionally; ``sendSubscribe`` no-ops while the socket
    // is closed and the ``onopen`` handler resubscribes everything in
    // ``this.subscribed`` so a fired tick during a transient disconnect
    // doesn't lose ground.
    const handle = window.setInterval(() => {
      if (!this.subscribed.has(conversationId)) {
        this.clearRefreshTimer(conversationId);
        return;
      }
      this.sendSubscribe(conversationId);
    }, SUBSCRIPTION_REFRESH_INTERVAL_MS);
    this.subscriptionRefreshTimers.set(conversationId, handle);
  }

  private clearRefreshTimer(conversationId: string): void {
    const existing = this.subscriptionRefreshTimers.get(conversationId);
    if (existing !== undefined) {
      window.clearInterval(existing);
      this.subscriptionRefreshTimers.delete(conversationId);
    }
  }

  private clearAllRefreshTimers(): void {
    for (const handle of this.subscriptionRefreshTimers.values()) {
      window.clearInterval(handle);
    }
    this.subscriptionRefreshTimers.clear();
  }

  private sendSubscribe(conversationId: string): void {
    const lastSeq = this.lastSeqByConversation.get(conversationId) ?? 0;
    this.send({
      op: 'subscribe',
      conversation_id: conversationId,
      last_seq: lastSeq,
    });
  }

  onGlobalEvent(handler: GlobalHandler): () => void {
    this.globalHandlers.add(handler);
    return () => {
      this.globalHandlers.delete(handler);
    };
  }

  onConversationEvent(
    conversationId: string,
    handler: ConversationHandler,
  ): () => void {
    let set = this.conversationHandlers.get(conversationId);
    if (!set) {
      set = new Set();
      this.conversationHandlers.set(conversationId, set);
    }
    set.add(handler);
    return () => {
      const cur = this.conversationHandlers.get(conversationId);
      if (!cur) return;
      cur.delete(handler);
      if (cur.size === 0) {
        this.conversationHandlers.delete(conversationId);
      }
    };
  }

  setLastSeq(conversationId: string, seq: number): void {
    this.recordLastSeq(conversationId, seq);
  }

  getLastSeq(conversationId: string): number {
    return this.lastSeqByConversation.get(conversationId) ?? 0;
  }

  // ------------------------------------------------------------------
  // sessionStorage persistence for last_seq
  // ------------------------------------------------------------------

  private recordLastSeq(conversationId: string, seq: number): void {
    const cur = this.lastSeqByConversation.get(conversationId) ?? 0;
    if (seq <= cur) return;
    this.lastSeqByConversation.set(conversationId, seq);
    this.persistLastSeqMap();
  }

  private loadLastSeqMap(): Map<string, number> {
    try {
      const raw = sessionStorage.getItem(LAST_SEQ_STORAGE_KEY);
      if (!raw) return new Map();
      const obj = JSON.parse(raw) as Record<string, number>;
      return new Map(Object.entries(obj));
    } catch {
      return new Map();
    }
  }

  private persistLastSeqMap(): void {
    try {
      const obj: Record<string, number> = {};
      this.lastSeqByConversation.forEach((seq, id) => {
        obj[id] = seq;
      });
      sessionStorage.setItem(LAST_SEQ_STORAGE_KEY, JSON.stringify(obj));
    } catch {
      // Quota or unavailable storage -- non-fatal; the in-memory map still works.
    }
  }
}

export const persistentWebSocket = new PersistentWebSocketClient();
