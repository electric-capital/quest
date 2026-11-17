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
 * A project shows the dot while any of its conversations is unread. The
 * project list names each project's unread conversations as of the fetch,
 * and the event names the project of the conversation it is about, so the
 * store can follow a project's conversations it has never listed.
 *
 * The store also knows which conversation the main view is showing. When
 * that conversation is unread and the window is focused it acknowledges at
 * once (optimistically, then POST /conversations/{id}/read), which is what
 * clears the dot on every other client. An unfocused window acknowledges
 * when it regains focus (`acknowledgeViewed`, wired to the focus events by
 * hooks/useConversationUnread.ts).
 *
 * AutoProjects follow the same rules with their own pair of seqs (unread
 * while `unread_seq > read_seq`), kept in separate maps so an AutoProject id can
 * never be read as a conversation id. AutoProject seqs come from list rows, the
 * AutoProject detail and the read response only: there is no AutoProject unread event;
 * `autoproject_list_changed` / `autoproject_changed` make the AutoProject hooks refetch, and
 * they merge the refetched rows and detail here (hooks/useAutoProjects.ts,
 * hooks/useAutoProject.ts). The viewed AutoProject (its newest update on screen, reported
 * by hooks/useAutoProjectUnread.ts) is acknowledged by `acknowledgeViewed` exactly
 * like the viewed conversation, through POST /autoprojects/{id}/read.
 */

import { markConversationRead } from '../api/client';
import { markAutoProjectRead } from '../api/autoprojectsApi';
import type { UnreadConversationRef } from '../api/types';

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
  // Conversation id -> project id, for conversations a report placed in a
  // project. A conversation never leaves its project.
  private projectIds = new Map<string, string>();
  private listeners = new Set<Listener>();
  private viewedId: string | null = null;
  // AutoProjects: the same three pieces of state, keyed by AutoProject id.
  private autoprojectSeqs = new Map<string, UnreadSeqs>();
  private autoprojectPendingAcks = new Map<string, number>();
  private viewedAutoProjectId: string | null = null;
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

  /**
   * Whether the project has an unread conversation: one of the rows its
   * list entry named, or one a later report placed in the project.
   */
  isProjectUnread(projectId: string, rows: UnreadConversationRef[] = []): boolean {
    if (rows.some((row) => this.isUnread(row.id, row.unread_seq, row.read_seq))) {
      return true;
    }
    for (const [id, conversationProjectId] of this.projectIds) {
      if (conversationProjectId === projectId && this.isUnread(id)) return true;
    }
    return false;
  }

  /**
   * Fold in seqs reported by the server (any source, any order), with the
   * conversation's project when the report names one.
   */
  merge(id: string, unreadSeq: unknown, readSeq: unknown, projectId?: unknown): void {
    const current = this.seqs.get(id);
    const next = {
      unreadSeq: Math.max(current?.unreadSeq ?? 0, toSeq(unreadSeq)),
      readSeq: Math.max(current?.readSeq ?? 0, toSeq(readSeq)),
    };
    const placed = typeof projectId === 'string' && projectId !== ''
      && this.projectIds.get(id) !== projectId;
    if (placed) this.projectIds.set(id, projectId);
    if (
      !placed && current
      && current.unreadSeq === next.unreadSeq && current.readSeq === next.readSeq
    ) {
      return;
    }
    this.seqs.set(id, next);
    // Before notifying: when this is the conversation on screen, listeners
    // never observe it as unread.
    if (id === this.viewedId) this.acknowledgeViewedConversation();
    this.emit();
  }

  /** The main view now shows this conversation (loaded). */
  setViewed(id: string): void {
    this.viewedId = id;
    this.acknowledgeViewedConversation();
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
   * Whether the AutoProject is unread, given the seqs on the caller's own copy of
   * its row or detail.
   */
  isAutoProjectUnread(id: string, rowUnreadSeq: unknown = 0, rowReadSeq: unknown = 0): boolean {
    const known = this.autoprojectSeqs.get(id);
    const unreadSeq = Math.max(toSeq(rowUnreadSeq), known?.unreadSeq ?? 0);
    const readSeq = Math.max(
      toSeq(rowReadSeq),
      known?.readSeq ?? 0,
      this.autoprojectPendingAcks.get(id) ?? 0,
    );
    return unreadSeq > readSeq;
  }

  /** Fold in an AutoProject's seqs reported by the server (row, detail, read response). */
  mergeAutoProject(id: string, unreadSeq: unknown, readSeq: unknown): void {
    const current = this.autoprojectSeqs.get(id);
    const next = {
      unreadSeq: Math.max(current?.unreadSeq ?? 0, toSeq(unreadSeq)),
      readSeq: Math.max(current?.readSeq ?? 0, toSeq(readSeq)),
    };
    if (current && current.unreadSeq === next.unreadSeq && current.readSeq === next.readSeq) {
      return;
    }
    this.autoprojectSeqs.set(id, next);
    // Before notifying, as for conversations.
    if (id === this.viewedAutoProjectId) this.acknowledgeViewedAutoProject();
    this.emit();
  }

  /** The main view now shows this AutoProject's thread (loaded). */
  setViewedAutoProject(id: string): void {
    this.viewedAutoProjectId = id;
    this.acknowledgeViewedAutoProject();
  }

  /** The main view no longer shows this AutoProject's thread. */
  clearViewedAutoProject(id: string): void {
    if (this.viewedAutoProjectId === id) this.viewedAutoProjectId = null;
  }

  /**
   * Acknowledge the viewed conversation and the viewed AutoProject, each if it is
   * unread and the window is focused. Called whenever any of the three can
   * have changed.
   */
  acknowledgeViewed(): void {
    this.acknowledgeViewedConversation();
    this.acknowledgeViewedAutoProject();
  }

  private acknowledgeViewedConversation(): void {
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

  private acknowledgeViewedAutoProject(): void {
    const id = this.viewedAutoProjectId;
    if (!id || !this.acknowledging || !isWindowFocused()) return;
    const known = this.autoprojectSeqs.get(id);
    if (!known || !this.isAutoProjectUnread(id)) return;
    const seq = known.unreadSeq;
    this.autoprojectPendingAcks.set(id, seq);
    this.emit();
    markAutoProjectRead(id, seq)
      .then((result) => {
        this.mergeAutoProject(id, result.unread_seq, result.read_seq);
        this.settleAutoProjectAck(id, seq);
      })
      .catch(() => {
        // Unacknowledged: the dot comes back, and the next focus change or
        // refetch of this AutoProject retries.
        this.settleAutoProjectAck(id, seq);
      });
  }

  private settleAutoProjectAck(id: string, seq: number): void {
    if (this.autoprojectPendingAcks.get(id) !== seq) return;
    this.autoprojectPendingAcks.delete(id);
    this.emit();
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
    this.projectIds.clear();
    this.viewedId = null;
    this.autoprojectSeqs.clear();
    this.autoprojectPendingAcks.clear();
    this.viewedAutoProjectId = null;
    this.acknowledging = true;
    this.emit();
  }
}

export const unreadStore = new UnreadStore();
