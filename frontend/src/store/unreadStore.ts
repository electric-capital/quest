/**
 * Unread state of conversations, behind the sidebar's unread dot.
 *
 * The server keeps two monotonic message seqs per conversation: `unread_seq`
 * (the high-water seq when the latest run ended) and `read_seq` (the highest
 * seq a focused client showing the conversation has acknowledged). A
 * conversation is unread while `unread_seq > read_seq`.
 *
 * Both seqs reach the client from several places that can race: list rows,
 * the conversation detail, `subscribed` acks and the
 * `conversation_unread_changed` event. Because they only move forward, the
 * store folds every report in with `max`, so a stale list fetch that lands
 * after a newer event cannot bring a dot back.
 *
 * The store also knows which conversation the main view is showing. When
 * that conversation is unread and the window is focused it acknowledges at
 * once (optimistically, then POST /conversations/{id}/read), which is what
 * clears the dot on every other client. An unfocused window acknowledges
 * when it regains focus (`acknowledgeViewed`, wired to the focus events by
 * hooks/useConversationUnread.ts).
 */

import { markConversationRead } from '../api/client';

interface UnreadSeqs {
  unreadSeq: number;
  readSeq: number;
}

type Listener = () => void;

function toSeq(value: unknown): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : 0;
}

/** The window is on screen and has input focus: the user can see it. */
function isWindowFocused(): boolean {
  return document.visibilityState === 'visible' && document.hasFocus();
}

class UnreadStore {
  // Server-reported seqs, merged with max.
  private seqs = new Map<string, UnreadSeqs>();
  // Acknowledgements in flight: the seq sent, counted as read until the
  // request settles so the dot clears without waiting for the round trip.
  private pendingAcks = new Map<string, number>();
  private listeners = new Set<Listener>();
  private viewedId: string | null = null;
  // False under admin impersonation: the admin looking at a conversation
  // is not its owner reading it (the server refuses the request as well).
  private acknowledging = true;

  subscribe = (listener: Listener): (() => void) => {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  };

  /**
   * Whether the conversation is unread, given the seqs on the caller's own
   * copy of the row (a list row can be newer or older than the store).
   */
  isUnread(id: string, rowUnreadSeq: unknown = 0, rowReadSeq: unknown = 0): boolean {
    const known = this.seqs.get(id);
    const unreadSeq = Math.max(toSeq(rowUnreadSeq), known?.unreadSeq ?? 0);
    const readSeq = Math.max(
      toSeq(rowReadSeq),
      known?.readSeq ?? 0,
      this.pendingAcks.get(id) ?? 0,
    );
    return unreadSeq > readSeq;
  }

  /** Fold in seqs reported by the server (any source, any order). */
  merge(id: string, unreadSeq: unknown, readSeq: unknown): void {
    const current = this.seqs.get(id);
    const next = {
      unreadSeq: Math.max(current?.unreadSeq ?? 0, toSeq(unreadSeq)),
      readSeq: Math.max(current?.readSeq ?? 0, toSeq(readSeq)),
    };
    if (current && current.unreadSeq === next.unreadSeq && current.readSeq === next.readSeq) {
      return;
    }
    this.seqs.set(id, next);
    // Before notifying: when this is the conversation on screen, listeners
    // never observe it as unread.
    if (id === this.viewedId) this.acknowledgeViewed();
    this.emit();
  }

  /** The main view now shows this conversation (loaded). */
  setViewed(id: string): void {
    this.viewedId = id;
    this.acknowledgeViewed();
  }

  /** The main view no longer shows this conversation. */
  clearViewed(id: string): void {
    if (this.viewedId === id) this.viewedId = null;
  }

  /** Turn acknowledging on or off for this session. */
  setAcknowledging(enabled: boolean): void {
    this.acknowledging = enabled;
    if (enabled) this.acknowledgeViewed();
  }

  /**
   * Acknowledge the viewed conversation if it is unread and the window is
   * focused. Called whenever any of the three can have changed.
   */
  acknowledgeViewed(): void {
    const id = this.viewedId;
    if (!id || !this.acknowledging || !isWindowFocused()) return;
    const known = this.seqs.get(id);
    if (!known || !this.isUnread(id)) return;
    const seq = known.unreadSeq;
    this.pendingAcks.set(id, seq);
    this.emit();
    markConversationRead(id, seq)
      .then((result) => {
        this.merge(id, result.unread_seq, result.read_seq);
        this.settleAck(id, seq);
      })
      .catch(() => {
        // Unacknowledged: the dot comes back, and the next focus change or
        // report for this conversation retries.
        this.settleAck(id, seq);
      });
  }

  private settleAck(id: string, seq: number): void {
    if (this.pendingAcks.get(id) !== seq) return;
    this.pendingAcks.delete(id);
    this.emit();
  }

  private emit(): void {
    this.listeners.forEach((listener) => listener());
  }

  /** Test helper: forget everything. */
  reset(): void {
    this.seqs.clear();
    this.pendingAcks.clear();
    this.viewedId = null;
    this.acknowledging = true;
    this.emit();
  }
}

export const unreadStore = new UnreadStore();
