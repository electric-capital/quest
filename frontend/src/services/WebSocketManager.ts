/**
 * WebSocket Manager (Phase 3 thin wrapper).
 *
 * The manager used to own a per-turn WebSocket per conversation. With the
 * persistent multiplexed socket from devplan 00062, all chat traffic
 * (subscribe, send_message, stop, transient text deltas, durable
 * message_appended) rides on the singleton ``persistentWebSocket`` --
 * this class is now a small compatibility shim that:
 *
 * 1. Bundles streaming text deltas into a synthetic ``text`` message at
 *    the next ``message_appended`` boundary, so the existing
 *    ``streamingMessages`` UI keeps working.
 * 2. Exposes the legacy callback API (``onStreamComplete``,
 *    ``onConversationRenamed``) so unrelated callers keep working without
 *    a sweeping refactor.
 * 3. Forwards ``sendMessage`` / ``stopStreaming`` calls to the
 *    persistent WS.
 */

import { conversationStore } from '../store/conversationStore';
import type {
  MessageContent,
  SubAgentToolUseMessage,
  SubAgentToolResultMessage,
  ComposerAttachmentRef,
} from '../api/types';
import {
  requestNotificationPermission,
  sendDesktopNotification,
} from './desktopNotifications';
import { persistentWebSocket, type PersistentEvent } from './PersistentWebSocket';

interface BufferState {
  responseParts: string[];
  structuredMessages: MessageContent[];
  isInterrupted: boolean;
  isStreaming: boolean;
  unsubscribe: () => void;
}

type StreamCompleteCallback = (conversationId: string) => void;
type ConversationRenamedCallback = (conversationId: string, customName: string) => void;

/**
 * An outbound ``send_message`` whose ``send_message_accepted`` receipt has
 * not arrived yet, or that failed and is parked for Retry / Discard. Keyed
 * by ``client_send_id`` so a receipt / Retry can find it without guessing
 * from message content.
 */
interface PendingSend {
  conversationId: string;
  clientSendId: string;
  /** The exact envelope that was (or should be) written to the socket. */
  payload: Record<string, unknown>;
  ackTimer: number | null;
  /**
   * While parked in the failed state the transient buffer (and its event
   * subscription) is gone, so a late receipt needs its own listener to
   * flip the bubble back. Null while in flight or untracked.
   */
  lateAckUnsubscribe: (() => void) | null;
}

/**
 * How long to wait for the server's ``send_message_accepted`` receipt
 * before declaring a send unconfirmed. The server appends the user row
 * synchronously on receipt, so on a healthy path the receipt arrives well
 * within a second or two; a longer silence means the frame most likely
 * never left the device (half-open mobile socket, radio off). The
 * receipt is still honored if it lands late -- the bubble flips back out
 * of the failed state.
 */
const SEND_ACK_TIMEOUT_MS = 15_000;

class WebSocketManagerClass {
  private buffers: Map<string, BufferState> = new Map();
  private pendingSends: Map<string, PendingSend> = new Map();
  private streamCompleteCallbacks: Set<StreamCompleteCallback> = new Set();
  private conversationRenamedCallbacks: Set<ConversationRenamedCallback> = new Set();

  // ------------------------------------------------------------------
  // Public callback subscriptions
  // ------------------------------------------------------------------

  onStreamComplete(callback: StreamCompleteCallback): () => void {
    this.streamCompleteCallbacks.add(callback);
    return () => this.streamCompleteCallbacks.delete(callback);
  }

  onConversationRenamed(callback: ConversationRenamedCallback): () => void {
    this.conversationRenamedCallbacks.add(callback);
    return () => this.conversationRenamedCallbacks.delete(callback);
  }

  private notifyStreamComplete(conversationId: string): void {
    this.streamCompleteCallbacks.forEach((cb) => cb(conversationId));
  }

  private notifyConversationRenamed(conversationId: string, customName: string): void {
    this.conversationRenamedCallbacks.forEach((cb) => cb(conversationId, customName));
  }

  // ------------------------------------------------------------------
  // Send / stop
  // ------------------------------------------------------------------

  sendMessage(
    conversationId: string,
    message: string,
    model?: string,
    guideId?: string,
    skillIds?: string[],
    attachments?: ComposerAttachmentRef[],
    flags?: string[],
    attachedFilenames?: string[],
    clientSendId?: string,
    thinkingEffort?: string | null,
  ): void {
    requestNotificationPermission();

    const payload: Record<string, unknown> = {
      op: 'send_message',
      conversation_id: conversationId,
      message,
      timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
      ...(model && { model }),
      // The thinking level picked for the model; omitted = its default.
      ...(model && thinkingEffort && { thinking_effort: thinkingEffort }),
      ...(guideId && { guide_id: guideId }),
      ...(skillIds && skillIds.length > 0 && { skill_ids: skillIds }),
      ...(attachments && attachments.length > 0 && { attachments }),
      ...(flags && flags.length > 0 && { flags }),
      ...(attachedFilenames && attachedFilenames.length > 0 && { attached_filenames: attachedFilenames }),
      // Set after the user clicks through the expensive-resume warning card;
      // the server-side gate rejects unacknowledged sends into long-idle,
      // long-context conversations otherwise.
      ...(conversationStore.isExpensiveResumeAcknowledged(conversationId)
        && { expensive_resume_acknowledged: true }),
      // Echoed back on ``send_message_accepted`` so the receipt can be
      // matched to the optimistic bubble the caller stamped with it.
      ...(clientSendId && { client_send_id: clientSendId }),
    };

    if (!clientSendId) {
      // Legacy caller without an optimistic bubble to track: fire and
      // forget as before, but at least roll the streaming state back when
      // the socket is known to be closed.
      conversationStore.startStreaming(conversationId);
      this.startBuffer(conversationId);
      if (!persistentWebSocket.send(payload)) {
        this.abandonStreaming(conversationId);
        conversationStore.setError(conversationId, 'Not connected to the server. Your message was not sent.');
      }
      return;
    }

    // Replace any earlier tracking for the same id (a Retry re-enters here
    // through dispatchPendingSend, never through this method).
    this.clearPendingSend(clientSendId);
    this.dispatchPendingSend({
      conversationId,
      clientSendId,
      payload,
      ackTimer: null,
      lateAckUnsubscribe: null,
    });
  }

  /**
   * Re-send a failed message. The optimistic bubble stays in place (text
   * preserved) and leaves the failed state while the retry is in flight.
   */
  retryFailedSend(conversationId: string, clientSendId: string): void {
    const pending = this.pendingSends.get(clientSendId);
    if (!pending || pending.conversationId !== conversationId) return;
    if (conversationStore.isStreaming(conversationId)) return;
    if (pending.ackTimer !== null) return;  // already in flight
    conversationStore.setSendFailed(conversationId, clientSendId, undefined);
    this.dispatchPendingSend(pending);
  }

  /** Drop a failed message: the bubble disappears and nothing is sent. */
  discardFailedSend(conversationId: string, clientSendId: string): void {
    this.clearPendingSend(clientSendId);
    conversationStore.removeOptimisticUserMessage(conversationId, clientSendId);
  }

  /**
   * Write a tracked send to the socket and arm its receipt timer. Both the
   * first attempt and every Retry come through here.
   */
  private dispatchPendingSend(pending: PendingSend): void {
    const { conversationId, clientSendId } = pending;
    this.pendingSends.set(clientSendId, pending);
    this.stopLateAckListener(pending);

    // Reset streaming state in store and start a fresh transient buffer.
    conversationStore.startStreaming(conversationId);
    this.startBuffer(conversationId);

    // ``send`` is false when the socket is closed (offline, reconnect
    // backoff in progress). Nothing was written, so fail immediately
    // instead of leaving the bubble and the stop button hanging until a
    // reload wipes them.
    if (!persistentWebSocket.send(pending.payload)) {
      this.failPendingSend(pending, 'not_connected');
      return;
    }

    // The frame is in the browser's buffer, which is NOT proof it reached
    // the server (a half-open socket accepts writes for up to the watchdog
    // deadline). Wait for the explicit receipt.
    pending.ackTimer = window.setTimeout(() => {
      pending.ackTimer = null;
      if (this.pendingSends.get(clientSendId) !== pending) return;
      // The row may have arrived through a catchup / tail-fetch even though
      // the receipt itself was lost; in that case the bubble is already
      // reconciled (stamped with a seq) and the run is genuinely live.
      if (!conversationStore.hasPendingSend(conversationId, clientSendId)) {
        this.pendingSends.delete(clientSendId);
        return;
      }
      this.failPendingSend(pending, 'unconfirmed');
    }, SEND_ACK_TIMEOUT_MS);
  }

  private failPendingSend(pending: PendingSend, reason: 'not_connected' | 'unconfirmed'): void {
    if (pending.ackTimer !== null) {
      window.clearTimeout(pending.ackTimer);
      pending.ackTimer = null;
    }
    // Keep the entry so Retry can re-send the identical envelope.
    conversationStore.setSendFailed(pending.conversationId, pending.clientSendId, reason);
    this.abandonStreaming(pending.conversationId);
    // abandonStreaming tore down the buffer subscription; keep one ear open
    // for a receipt that was merely slow rather than lost.
    pending.lateAckUnsubscribe = persistentWebSocket.onConversationEvent(
      pending.conversationId,
      (event) => {
        if (event.type === 'send_message_accepted') {
          this.handleSendAccepted(pending.conversationId, event);
        }
      },
    );
  }

  private stopLateAckListener(pending: PendingSend): void {
    if (pending.lateAckUnsubscribe === null) return;
    try {
      pending.lateAckUnsubscribe();
    } catch {
      // ignore
    }
    pending.lateAckUnsubscribe = null;
  }

  /**
   * Leave the streaming state without the synthetic "interrupted" marker a
   * real stop adds -- nothing ran. Mirrors the ``send_message_rejected``
   * teardown.
   */
  private abandonStreaming(conversationId: string): void {
    conversationStore.endStreaming(conversationId);
    this.endBuffer(conversationId);
    this.notifyStreamComplete(conversationId);
  }

  private clearPendingSend(clientSendId: string): void {
    const pending = this.pendingSends.get(clientSendId);
    if (!pending) return;
    if (pending.ackTimer !== null) {
      window.clearTimeout(pending.ackTimer);
      pending.ackTimer = null;
    }
    this.stopLateAckListener(pending);
    this.pendingSends.delete(clientSendId);
  }

  /**
   * The server rejected the send that is in flight on this conversation:
   * drop its bubble and tracking. Parked failed sends (timer disarmed,
   * awaiting Retry / Discard) are untouched -- the rejection was not about
   * them. Returns false when no tracked send was in flight so the caller
   * can fall back to the untracked-bubble cleanup.
   */
  private rejectInFlightSend(conversationId: string): boolean {
    let handled = false;
    for (const pending of Array.from(this.pendingSends.values())) {
      if (pending.conversationId !== conversationId || pending.ackTimer === null) continue;
      conversationStore.removeOptimisticUserMessage(conversationId, pending.clientSendId);
      this.clearPendingSend(pending.clientSendId);
      handled = true;
    }
    return handled;
  }

  /**
   * Enter the streaming (stop-button) state for a run this tab did not
   * start: the headless wait-handle resume that follows an action-request
   * Approve / Revise / Deny. Reuses the same transient buffer as
   * ``sendMessage`` so text deltas render live and the resume's
   * ``send_message_finished`` tears the state down through the normal
   * path. No-op when a buffer is already active (a send from this tab).
   */
  attachToResumeStream(conversationId: string): void {
    const existing = this.buffers.get(conversationId);
    if (existing && existing.isStreaming) return;
    conversationStore.startStreaming(conversationId);
    this.startBuffer(conversationId);
  }

  /**
   * Reconcile the local streaming state with the server's view of the run,
   * from the ``run_active`` field on a ``subscribed`` ack.
   *
   * The run-lifecycle envelopes (``resume_started`` /
   * ``send_message_finished``) are transient: one published while this
   * tab's socket was down (network blip, watchdog reconnect, a server
   * stall long enough to trip the heartbeat) is gone for good. The final
   * messages still arrive through the reconnect ``catchup``, so without
   * this reconcile a run that ended during the gap left the "generating"
   * spinner and the locked composer in place until a page reload.
   *
   * - ``run_active: false`` while we are streaming: the run is over; drain
   *   the buffer exactly like a rejected/abandoned send (no synthetic
   *   "interrupted" marker -- a stopped run persists its own durable
   *   marker, which the catchup delivered). Skipped while a tracked send
   *   is still awaiting its ``send_message_accepted`` receipt: the hook
   *   subscribes and sends back-to-back, so the ack for that subscribe is
   *   computed before the server has registered the run.
   * - ``run_active: true`` while we are idle: a run this tab did not start
   *   (or lost track of across a reconnect / page load) is streaming;
   *   enter the stop-button state like a headless resume so the composer
   *   does not offer a doomed second send.
   *
   * Absent field (older server) = no-op.
   */
  syncRunState(conversationId: string, runActive: unknown): void {
    if (typeof runActive !== 'boolean') return;
    const buf = this.buffers.get(conversationId);
    if (runActive) {
      if (buf && buf.isStreaming) return;
      this.attachToResumeStream(conversationId);
      return;
    }
    if (this.hasInFlightSend(conversationId)) return;
    if (buf) {
      this.abandonStreaming(conversationId);
      return;
    }
    if (conversationStore.isStreaming(conversationId)) {
      conversationStore.endStreaming(conversationId);
      this.notifyStreamComplete(conversationId);
    }
  }

  /** A tracked send whose receipt timer is still armed. */
  private hasInFlightSend(conversationId: string): boolean {
    for (const pending of this.pendingSends.values()) {
      if (pending.conversationId === conversationId && pending.ackTimer !== null) {
        return true;
      }
    }
    return false;
  }

  stopStreaming(conversationId: string): void {
    const buf = this.buffers.get(conversationId);
    if (buf) buf.isInterrupted = true;
    persistentWebSocket.send({
      op: 'stop',
      conversation_id: conversationId,
    });
  }

  // ------------------------------------------------------------------
  // Internal: per-conversation streaming buffer
  // ------------------------------------------------------------------

  private startBuffer(conversationId: string): void {
    // Tear down any existing buffer first so we don't double-subscribe.
    this.endBuffer(conversationId);

    const state: BufferState = {
      responseParts: [],
      structuredMessages: [],
      isInterrupted: false,
      isStreaming: true,
      unsubscribe: () => {},
    };

    state.unsubscribe = persistentWebSocket.onConversationEvent(
      conversationId,
      (event) => this.handlePersistentEvent(conversationId, event),
    );
    this.buffers.set(conversationId, state);
  }

  private endBuffer(conversationId: string): void {
    const buf = this.buffers.get(conversationId);
    if (!buf) return;
    try {
      buf.unsubscribe();
    } catch {
      // ignore
    }
    this.buffers.delete(conversationId);
  }

  private handlePersistentEvent(
    conversationId: string,
    event: PersistentEvent,
  ): void {
    const type = event.type as string | undefined;

    if (type === 'send_message_accepted') {
      this.handleSendAccepted(conversationId, event);
      return;
    }

    if (type === 'subscribed') {
      // Handled here as well as in useConversation so a buffer whose
      // ChatPanel is unmounted (the user is on another screen) still
      // drains when a subscribe refresh reports the run is over.
      this.syncRunState(conversationId, event.run_active);
      return;
    }

    const buf = this.buffers.get(conversationId);
    if (!buf) return;

    if (type === 'text_delta') {
      const chunk = (event.content as string | undefined) || '';
      if (chunk) {
        buf.responseParts.push(chunk);
        conversationStore.updatePartialResponse(
          conversationId,
          buf.responseParts.join(''),
        );
      }
      return;
    }

    if (type === 'sub_agent_tool_use') {
      const toolUse: SubAgentToolUseMessage = {
        type: 'sub_agent_tool_use',
        parent_tool_id: (event.parent_tool_id as string) || '',
        agent_name: (event.agent_name as string) || '',
        tool_name: (event.tool_name as string) || 'unknown',
        tool_input:
          (event.tool_input as Record<string, unknown>)
          || (event.parameters as Record<string, unknown>)
          || {},
        tool_id: (event.tool_id as string) || '',
        intent_message: (event.intent_message as string) || '',
        nested_agent_id: event.nested_agent_id as string | undefined,
        nested_agent_name: event.nested_agent_name as string | undefined,
        nested_agent_model: event.nested_agent_model as string | undefined,
        nested_parent_id: event.nested_parent_id as string | undefined,
      };
      conversationStore.addSubAgentToolUse(conversationId, toolUse.parent_tool_id, {
        agentName: toolUse.agent_name,
        toolUse,
      });
      return;
    }

    if (type === 'sub_agent_tool_result') {
      const toolResult: SubAgentToolResultMessage = {
        type: 'sub_agent_tool_result',
        parent_tool_id: (event.parent_tool_id as string) || '',
        agent_name: (event.agent_name as string) || '',
        tool_id: (event.tool_id as string) || '',
        tool_output:
          (event.tool_output as string)
          || (event.output as string)
          || '',
        nested_agent_id: event.nested_agent_id as string | undefined,
        nested_agent_status: event.nested_agent_status as ('success' | 'error' | undefined),
        nested_parent_id: event.nested_parent_id as string | undefined,
      };
      conversationStore.updateSubAgentToolResult(
        conversationId,
        toolResult.parent_tool_id,
        toolResult.tool_id,
        toolResult,
      );
      return;
    }

    if (type === 'sub_agent_finished') {
      const status: 'success' | 'error' = event.status === 'error' ? 'error' : 'success';
      const nestedParentId = event.nested_parent_id as string | undefined;
      // A 2nd-level (grandchild) agent's finished event is keyed under the
      // nested-agent node id so its inner section resolves; a normal 1st-level
      // sub-agent keys under the parent tool id as before.
      conversationStore.markSubAgentFinished(
        conversationId,
        nestedParentId || ((event.parent_tool_id as string) || ''),
        (event.agent_name as string) || '',
        status,
        event.error as string | undefined,
      );
      return;
    }

    if (type === 'conversation_updated') {
      const customName = (event.custom_name as string) || '';
      this.notifyConversationRenamed(conversationId, customName);
      return;
    }

    if (type === 'message_appended') {
      // The model loop just flushed a durable boundary. If we were
      // streaming assistant text, finalize the bubble in-place into the
      // persisted messages list at the event's seq -- this swaps the
      // streaming bubble for a synthetic persisted entry in a single
      // render so the user doesn't see a remove-then-readd flash while
      // the tail-fetch is in flight. The synthetic is replaced by the
      // canonical row when the tail-fetch lands.
      buf.responseParts = [];
      const seq = event.seq as number | undefined;
      if (typeof seq === 'number') {
        conversationStore.finalizeStreamingTextInPlace(conversationId, seq);
      } else {
        conversationStore.updatePartialResponse(conversationId, '');
      }
      conversationStore.setStreamingMessages(conversationId, []);
      return;
    }

    if (type === 'stop_acknowledged') {
      // The server confirmed our stop request; the actual end-of-run
      // signal still comes from send_message_finished below.
      buf.isInterrupted = true;
      return;
    }

    if (type === 'send_message_finished') {
      const interrupted = !!event.interrupted;
      // ``error: true`` means the run died server-side; the durable error
      // bubble arrives via message_appended, so here we only need to avoid
      // celebrating with the "Response complete" notification.
      const hadError = !!event.error;
      this.finishStream(conversationId, interrupted, hadError);
      return;
    }

    if (type === 'subscription_expired') {
      // The server expired our TTL while we were away from this conv; any
      // chunks emitted past that point were dropped on the wire. Clear the
      // streaming buffer so the next ``message_appended`` -> tail-fetch
      // path rebuilds canonical state instead of leaving a partial bubble.
      buf.responseParts = [];
      conversationStore.updatePartialResponse(conversationId, '');
      conversationStore.setStreamingMessages(conversationId, []);
      conversationStore.endStreaming(conversationId);
      this.endBuffer(conversationId);
      return;
    }

    if (type === 'send_message_rejected') {
      const reason = (event.reason as string | undefined) || '';
      let errorMessage: string;
      if (reason === 'attachments_unsupported_during_resume') {
        errorMessage = 'Pasted images cannot be sent while a tool call is awaiting resume. Send the images in a new message after the current run completes.';
      } else if (reason === 'expensive_resume_unacknowledged') {
        errorMessage = 'Resuming this conversation is expensive. Reload the page and acknowledge the cost warning to continue.';
      } else {
        errorMessage = 'A response is already in progress for this conversation.';
      }
      conversationStore.setError(conversationId, errorMessage);
      // Nothing ran and nothing was persisted: roll back the optimistic
      // user bubble and clear the streaming state WITHOUT the synthetic
      // "interrupted" marker a real stop would add -- a rejected send
      // interrupted nothing.
      if (!this.rejectInFlightSend(conversationId)) {
        conversationStore.removeTrailingOptimisticUserMessage(conversationId);
      }
      conversationStore.endStreaming(conversationId);
      this.endBuffer(conversationId);
      this.notifyStreamComplete(conversationId);
      return;
    }

    if (type === 'error') {
      const errorMsg = (event.error as string)
        || (event.message as string)
        || 'WebSocket error';
      conversationStore.setError(conversationId, errorMsg);
      return;
    }
  }

  /**
   * The server persisted our user row. Disarm the receipt timer; if the
   * receipt is late (the bubble was already flagged failed), clear the
   * failed state and re-enter streaming so the run's events render.
   */
  private handleSendAccepted(conversationId: string, event: PersistentEvent): void {
    const clientSendId = event.client_send_id as string | undefined;
    if (!clientSendId) return;
    const pending = this.pendingSends.get(clientSendId);
    if (!pending || pending.conversationId !== conversationId) return;
    const wasFailed = pending.ackTimer === null;
    this.clearPendingSend(clientSendId);
    if (wasFailed) {
      conversationStore.setSendFailed(conversationId, clientSendId, undefined);
      this.attachToResumeStream(conversationId);
    }
  }

  private finishStream(conversationId: string, fromStop: boolean, hadError = false): void {
    const buf = this.buffers.get(conversationId);
    if (!buf) return;
    if (buf.isInterrupted || fromStop) {
      const interruptedMsg: MessageContent = {
        type: 'interrupted',
        timestamp: new Date().toISOString(),
      };
      buf.structuredMessages.push(interruptedMsg);
    }
    conversationStore.endStreaming(conversationId, buf.structuredMessages);
    buf.isStreaming = false;
    if (!buf.isInterrupted && !fromStop && !hadError && buf.structuredMessages.length > 0) {
      sendDesktopNotification('Quest — Response complete');
    }
    this.endBuffer(conversationId);
    this.notifyStreamComplete(conversationId);
  }
}

export const webSocketManager = new WebSocketManagerClass();
