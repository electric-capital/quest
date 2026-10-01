/**
 * Observable store for per-conversation state
 * Uses useSyncExternalStore pattern for React integration
 */

import type { MessageContent, SubAgentToolResultMessage, SubAgentToolCallInfo, SubAgentFinishedInfo, PendingWaitInfo, ExpensiveResumeInfo } from '../api/types';

/**
 * State for a single conversation
 */
export interface ConversationState {
  messages: MessageContent[];
  isStreaming: boolean;
  partialResponse: string;
  streamingMessages: MessageContent[];
  error: string | null;
  stacktrace: string | undefined;
  isLoaded: boolean;
  isLoading: boolean;
  subAgentToolCalls: Map<string, SubAgentToolCallInfo[]>;
  // Per-parent-tool, per-agent terminal state. Outer key = parent_tool_id
  // (the agent_task / agent_task_parallel tool_id), inner key = agent_name.
  // Populated by sub_agent_finished events and drives the COMPLETED / ERRORED
  // badge for each sub-agent row.
  subAgentReturned: Map<string, Map<string, SubAgentFinishedInfo>>;
  contextTokens: number | null;
  maxContextTokens: number | null;
  // Wait handles the agent is suspended on. Non-empty locks the composer
  // regardless of streaming state. Seeded from ``pending_wait_handles`` on
  // the GET /conversations/:id payload, cleared by ``wait_handle_resolved``
  // events from the persistent WS.
  pendingWaitHandles: PendingWaitInfo[];
  // Server verdict that resuming this conversation is expensive (seeded from
  // ``expensive_resume`` on the GET payload). Non-null blocks the composer
  // behind a warning card until the user acknowledges below.
  expensiveResume: ExpensiveResumeInfo | null;
  // Set when the user clicks "Continue anyway" on the warning card. Rides
  // on the send_message envelope as ``expensive_resume_acknowledged`` so the
  // server-side gate lets the turn through.
  expensiveResumeAcknowledged: boolean;
}

/**
 * Create default state for a new conversation
 */
function createDefaultState(): ConversationState {
  return {
    messages: [],
    isStreaming: false,
    partialResponse: '',
    streamingMessages: [],
    error: null,
    stacktrace: undefined,
    isLoaded: false,
    isLoading: false,
    subAgentToolCalls: new Map(),
    subAgentReturned: new Map(),
    contextTokens: null,
    maxContextTokens: null,
    pendingWaitHandles: [],
    expensiveResume: null,
    expensiveResumeAcknowledged: false,
  };
}

type Listener = () => void;

/**
 * Conversation store - singleton that holds all per-conversation state
 */
class ConversationStore {
  private conversations: Map<string, ConversationState> = new Map();
  private listeners: Set<Listener> = new Set();
  private snapshotVersion: number = 0;

  /**
   * Get the state for a specific conversation
   */
  getConversation(id: string): ConversationState {
    let state = this.conversations.get(id);
    if (!state) {
      state = createDefaultState();
      this.conversations.set(id, state);
    }
    return state;
  }

  /**
   * Update state for a specific conversation
   */
  updateConversation(id: string, update: Partial<ConversationState>): void {
    const current = this.getConversation(id);
    const updated = { ...current, ...update };
    this.conversations.set(id, updated);
    this.notify();
  }

  /**
   * Set the messages for a conversation (typically after loading from API)
   */
  setMessages(id: string, messages: MessageContent[]): void {
    this.updateConversation(id, {
      messages,
      isLoaded: true,
      isLoading: false,
    });
  }

  /**
   * Append messages to a conversation (after streaming completes)
   */
  appendMessages(id: string, newMessages: MessageContent[]): void {
    const current = this.getConversation(id);
    this.updateConversation(id, {
      messages: [...current.messages, ...newMessages],
    });
  }

  /**
   * Add a single message (like user's input)
   */
  addMessage(id: string, message: MessageContent): void {
    const current = this.getConversation(id);
    this.updateConversation(id, {
      messages: [...current.messages, message],
    });
  }

  /**
   * Insert one or more server-stamped messages into the conversation at the
   * position dictated by their ``seq``, deduping against existing seqs.
   *
   * Tail-fetches triggered by separate ``message_appended`` events can resolve
   * out-of-order (smaller payload races ahead), so we cannot rely on append
   * order to match disk order. Inserting by seq keeps the rendered list in
   * monotonic-seq order regardless of which fetch lands first.
   *
   * Messages without a numeric ``seq`` (e.g. optimistic user bubbles that
   * haven't been reconciled yet) act as a trailing sentinel: any new seq'd
   * messages insert before them. This matches the runtime invariant that
   * optimistic placeholders are always at the tail.
   *
   * When an existing entry at the same seq is marked ``synthetic`` (a primary
   * tab finalized the streaming text into the messages list ahead of the
   * tail-fetch), the canonical row replaces it in place so the bubble keeps
   * its array index -- React reuses the same DOM node and the user doesn't
   * see the streaming -> persisted swap as a remount.
   */
  insertMessagesBySeq(id: string, newMessages: MessageContent[]): void {
    if (newMessages.length === 0) return;
    const current = this.getConversation(id);
    const seqIndex = new Map<number, number>();
    for (let i = 0; i < current.messages.length; i++) {
      const s = (current.messages[i] as unknown as { seq?: number }).seq;
      if (typeof s === 'number') seqIndex.set(s, i);
    }
    const merged = current.messages.slice();
    let changed = false;
    for (const incoming of newMessages) {
      const seq = (incoming as unknown as { seq?: number }).seq;
      if (typeof seq !== 'number') {
        merged.push(incoming);
        changed = true;
        continue;
      }
      const existingIdx = seqIndex.get(seq);
      if (typeof existingIdx === 'number') {
        const existing = merged[existingIdx] as unknown as { synthetic?: boolean };
        if (existing.synthetic === true) {
          merged[existingIdx] = incoming;
          changed = true;
        }
        continue;
      }
      // Linear scan from the end: find the first existing seq'd entry whose
      // seq is <= incoming.seq, and insert after it. Non-seq'd entries
      // (optimistic) at the tail always sort after seq'd entries.
      let insertAt = merged.length;
      for (let i = merged.length - 1; i >= 0; i--) {
        const existing = merged[i] as unknown as { seq?: number };
        if (typeof existing.seq !== 'number') continue;
        if (existing.seq <= seq) {
          insertAt = i + 1;
          break;
        }
        insertAt = i;
      }
      merged.splice(insertAt, 0, incoming);
      // Rebuild the index for entries at/after the insertion point so later
      // iterations of this loop see the correct positions.
      for (let i = insertAt; i < merged.length; i++) {
        const s = (merged[i] as unknown as { seq?: number }).seq;
        if (typeof s === 'number') seqIndex.set(s, i);
      }
      changed = true;
    }
    if (!changed) return;
    this.updateConversation(id, { messages: merged });
  }

  /**
   * Finalize an in-progress streaming text bubble into the persisted messages
   * list synchronously with clearing ``partialResponse``. Called on the primary
   * tab when ``message_appended`` lands for the assistant text we just streamed
   * -- inserting a synthetic text entry at the event's ``seq`` keeps the
   * bubble visually present at the same vertical slot during the tail-fetch
   * latency, eliminating the unmount-then-remount flash and the scroll jump
   * caused by a transient gap in the messages list.
   *
   * The synthetic entry is replaced in place by ``insertMessagesBySeq`` once
   * the canonical row arrives, so timestamps and any other server-stamped
   * fields end up correct without a second visible swap.
   */
  finalizeStreamingTextInPlace(id: string, seq: number): void {
    const current = this.getConversation(id);
    if (!current.partialResponse) return;
    const synthetic: MessageContent = {
      role: 'assistant',
      content: current.partialResponse,
      timestamp: new Date().toISOString(),
      type: 'text',
      seq,
      synthetic: true,
    } as MessageContent;
    const seqIndex = new Map<number, number>();
    for (let i = 0; i < current.messages.length; i++) {
      const s = (current.messages[i] as unknown as { seq?: number }).seq;
      if (typeof s === 'number') seqIndex.set(s, i);
    }
    const merged = current.messages.slice();
    if (seqIndex.has(seq)) {
      // Tail-fetch already won the race; nothing to inject. Just clear
      // the streaming buffer -- the canonical row is already visible.
      this.updateConversation(id, { partialResponse: '' });
      return;
    }
    let insertAt = merged.length;
    for (let i = merged.length - 1; i >= 0; i--) {
      const existing = merged[i] as unknown as { seq?: number };
      if (typeof existing.seq !== 'number') continue;
      if (existing.seq <= seq) {
        insertAt = i + 1;
        break;
      }
      insertAt = i;
    }
    merged.splice(insertAt, 0, synthetic);
    this.updateConversation(id, {
      messages: merged,
      partialResponse: '',
    });
  }

  /**
   * Reconcile a server-stamped user message with a prior optimistic one.
   *
   * When `sendMessage` adds a user bubble immediately for zero-latency UX, the
   * message has no ``seq`` and is tagged ``optimistic: true``. Once the server
   * persists the message and the client tail-fetches it, this replaces the
   * matching optimistic entry in-place (stamped with the real ``seq``) instead
   * of appending a duplicate. Returns true if a replacement happened, false if
   * no matching optimistic message was found (caller should append normally).
   */
  reconcileOptimisticUserMessage(id: string, serverMessage: MessageContent): boolean {
    const current = this.getConversation(id);
    const role = (serverMessage as unknown as { role?: string }).role;
    const content = (serverMessage as unknown as { content?: string }).content;
    if (role !== 'user' || typeof content !== 'string') return false;
    // Find the oldest optimistic user message with matching content. We match
    // oldest-first so two rapid sends stay in their original order.
    const idx = current.messages.findIndex((m) => {
      const mm = m as unknown as { role?: string; content?: string; optimistic?: boolean; seq?: number };
      return mm.optimistic === true && mm.role === 'user' && mm.content === content && mm.seq == null;
    });
    if (idx === -1) return false;
    const next = current.messages.slice();
    // Replace the optimistic placeholder with the server-authoritative message
    // so the bubble inherits its seq/timestamp without re-mounting.
    next[idx] = serverMessage;
    this.updateConversation(id, { messages: next });
    return true;
  }

  /**
   * Drop the most recent optimistic (not yet server-stamped) user message.
   * Called when the server rejects a send: the bubble was added
   * optimistically by ``sendMessage`` but no row was ever persisted, so
   * leaving it would show a ghost message that vanishes on reload.
   */
  removeTrailingOptimisticUserMessage(id: string): void {
    const current = this.getConversation(id);
    for (let i = current.messages.length - 1; i >= 0; i--) {
      const mm = current.messages[i] as unknown as { role?: string; optimistic?: boolean; seq?: number };
      if (mm.optimistic === true && mm.role === 'user' && mm.seq == null) {
        const next = current.messages.slice(0, i).concat(current.messages.slice(i + 1));
        this.updateConversation(id, { messages: next });
        return;
      }
    }
  }

  /**
   * Find the index of the still-unstamped optimistic user bubble carrying
   * ``client_send_id``; -1 when it was already reconciled or discarded.
   */
  private findPendingSendIndex(id: string, clientSendId: string): number {
    const current = this.getConversation(id);
    return current.messages.findIndex((m) => {
      const mm = m as unknown as { role?: string; optimistic?: boolean; seq?: number; client_send_id?: string };
      return mm.optimistic === true && mm.role === 'user' && mm.seq == null
        && mm.client_send_id === clientSendId;
    });
  }

  /** True while the bubble for ``clientSendId`` is still awaiting its server row. */
  hasPendingSend(id: string, clientSendId: string): boolean {
    return this.findPendingSendIndex(id, clientSendId) !== -1;
  }

  /**
   * Flip the optimistic bubble for ``clientSendId`` into the failed state
   * (or back out of it with ``reason`` undefined). No-op once the row has
   * been reconciled -- a late ``message_appended`` wins over a stale timer.
   */
  setSendFailed(id: string, clientSendId: string, reason: 'not_connected' | 'unconfirmed' | undefined): void {
    const idx = this.findPendingSendIndex(id, clientSendId);
    if (idx === -1) return;
    const current = this.getConversation(id);
    const existing = current.messages[idx] as unknown as { send_failed?: string };
    if (existing.send_failed === reason) return;
    const next = current.messages.slice();
    const updated = { ...(next[idx] as object) } as unknown as { send_failed?: string };
    if (reason) {
      updated.send_failed = reason;
    } else {
      delete updated.send_failed;
    }
    next[idx] = updated as unknown as MessageContent;
    this.updateConversation(id, { messages: next });
  }

  /** Drop the optimistic bubble for ``clientSendId`` (the user chose Discard). */
  removeOptimisticUserMessage(id: string, clientSendId: string): void {
    const idx = this.findPendingSendIndex(id, clientSendId);
    if (idx === -1) return;
    const current = this.getConversation(id);
    const next = current.messages.slice(0, idx).concat(current.messages.slice(idx + 1));
    this.updateConversation(id, { messages: next });
  }

  /**
   * Start streaming for a conversation
   */
  startStreaming(id: string): void {
    this.updateConversation(id, {
      isStreaming: true,
      partialResponse: '',
      streamingMessages: [],
      error: null,
      stacktrace: undefined,
    });
  }

  /**
   * Update streaming state (partial response text)
   */
  updatePartialResponse(id: string, partialResponse: string): void {
    this.updateConversation(id, { partialResponse });
  }

  /**
   * Update streaming messages (structured messages during stream)
   */
  setStreamingMessages(id: string, streamingMessages: MessageContent[]): void {
    this.updateConversation(id, { streamingMessages });
  }

  /**
   * End streaming for a conversation
   */
  endStreaming(id: string, finalMessages?: MessageContent[]): void {
    const current = this.getConversation(id);

    if (finalMessages && finalMessages.length > 0) {
      this.updateConversation(id, {
        isStreaming: false,
        partialResponse: '',
        streamingMessages: [],
        messages: [...current.messages, ...finalMessages],
      });
    } else {
      this.updateConversation(id, {
        isStreaming: false,
        partialResponse: '',
        streamingMessages: [],
      });
    }
  }

  /**
   * Set error state for a conversation
   */
  setError(id: string, error: string, stacktrace?: string): void {
    this.updateConversation(id, {
      error,
      stacktrace,
      isStreaming: false,
    });
  }

  /**
   * Set loading state for a conversation
   */
  setLoading(id: string, isLoading: boolean): void {
    this.updateConversation(id, { isLoading });
  }

  /**
   * Clear error state for a conversation
   */
  clearError(id: string): void {
    this.updateConversation(id, {
      error: null,
      stacktrace: undefined,
    });
  }

  /**
   * Add a sub-agent tool use event (display-only, transient)
   */
  addSubAgentToolUse(id: string, parentToolId: string, info: SubAgentToolCallInfo): void {
    const state = this.getConversation(id);
    const newMap = new Map(state.subAgentToolCalls);
    const existing = newMap.get(parentToolId) || [];
    newMap.set(parentToolId, [...existing, info]);
    this.updateConversation(id, { subAgentToolCalls: newMap });
  }

  /**
   * Attach a result to a matching sub-agent tool use event
   */
  updateSubAgentToolResult(id: string, parentToolId: string, toolId: string, result: SubAgentToolResultMessage): void {
    const state = this.getConversation(id);
    const newMap = new Map(state.subAgentToolCalls);
    const entries = newMap.get(parentToolId);
    if (entries) {
      const updated = entries.map(entry =>
        entry.toolUse.tool_id === toolId
          ? { ...entry, toolResult: result }
          : entry
      );
      newMap.set(parentToolId, updated);
      this.updateConversation(id, { subAgentToolCalls: newMap });
    }
  }

  /**
   * Mark a sub-agent as finished. The sub_agent_finished event is the
   * canonical "this sub-agent has called agent_task_response (or errored)"
   * signal from the backend -- it is NOT inferred from whether the last
   * inner tool call has a result.
   */
  markSubAgentFinished(
    id: string,
    parentToolId: string,
    agentName: string,
    status: 'success' | 'error',
    error?: string,
  ): void {
    const state = this.getConversation(id);
    const newOuter = new Map(state.subAgentReturned);
    const existingInner = newOuter.get(parentToolId);
    const newInner = existingInner ? new Map(existingInner) : new Map<string, SubAgentFinishedInfo>();
    newInner.set(agentName, error !== undefined ? { status, error } : { status });
    newOuter.set(parentToolId, newInner);
    this.updateConversation(id, { subAgentReturned: newOuter });
  }

  /**
   * Clear all sub-agent tool calls (called when streaming ends)
   */
  clearSubAgentToolCalls(id: string): void {
    this.updateConversation(id, {
      subAgentToolCalls: new Map(),
      subAgentReturned: new Map(),
    });
  }

  /**
   * Set context usage for a conversation (from stats events)
   */
  setContextUsage(id: string, contextTokens: number, maxContextTokens: number): void {
    this.updateConversation(id, { contextTokens, maxContextTokens });
  }

  /**
   * Delete a conversation from the store
   */
  deleteConversation(id: string): void {
    this.conversations.delete(id);
    this.notify();
  }

  /**
   * Check if a conversation is currently streaming
   */
  isStreaming(id: string): boolean {
    return this.getConversation(id).isStreaming;
  }

  /**
   * Replace the pending wait handles list for a conversation. Called when
   * the conversation detail payload is loaded; ``wait_handle_resolved``
   * events trim individual entries via ``removePendingWaitHandle``.
   */
  setPendingWaitHandles(id: string, handles: PendingWaitInfo[]): void {
    this.updateConversation(id, { pendingWaitHandles: handles });
  }

  /**
   * Remove a single pending wait handle by id. Called on
   * ``wait_handle_resolved``. No-op if the id is not currently tracked.
   */
  removePendingWaitHandle(id: string, handleId: string): void {
    const current = this.getConversation(id);
    if (current.pendingWaitHandles.length === 0) return;
    const next = current.pendingWaitHandles.filter((h) => h.id !== handleId);
    if (next.length === current.pendingWaitHandles.length) return;
    this.updateConversation(id, { pendingWaitHandles: next });
  }

  /**
   * Whether the conversation has any pending wait handle.
   */
  hasPendingWaitHandle(id: string): boolean {
    return this.getConversation(id).pendingWaitHandles.length > 0;
  }

  /**
   * Set (or clear) the expensive-resume verdict for a conversation. Called
   * when the conversation detail payload is loaded. Leaves the acknowledged
   * flag alone so a background resync after "Continue anyway" doesn't
   * re-lock the composer mid-session.
   */
  setExpensiveResume(id: string, info: ExpensiveResumeInfo | null): void {
    this.updateConversation(id, { expensiveResume: info });
  }

  /**
   * Record that the user clicked through the expensive-resume warning.
   * Unlocks the composer and makes subsequent sends carry the
   * ``expensive_resume_acknowledged`` flag.
   */
  acknowledgeExpensiveResume(id: string): void {
    this.updateConversation(id, { expensiveResumeAcknowledged: true });
  }

  /**
   * Whether the user has acknowledged this conversation's resume cost.
   */
  isExpensiveResumeAcknowledged(id: string): boolean {
    return this.getConversation(id).expensiveResumeAcknowledged;
  }

  /**
   * Whether sends should be blocked behind the expensive-resume warning.
   */
  isExpensiveResumeBlocked(id: string): boolean {
    const state = this.getConversation(id);
    return state.expensiveResume !== null && !state.expensiveResumeAcknowledged;
  }

  /**
   * Subscribe to store changes (for useSyncExternalStore)
   */
  subscribe(listener: Listener): () => void {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  }

  /**
   * Get a snapshot of the store (for useSyncExternalStore)
   * Returns a stable number that changes when store updates
   */
  getSnapshot(): number {
    return this.snapshotVersion;
  }

  /**
   * Get a per-conversation snapshot (for useSyncExternalStore).
   * Returns the conversation state reference, which only changes when that
   * specific conversation is updated. This allows hooks watching a single
   * conversation to skip re-renders caused by updates to other conversations.
   */
  getConversationSnapshot(id: string): ConversationState {
    return this.getConversation(id);
  }

  /**
   * Notify all listeners of changes
   */
  private notify(): void {
    this.snapshotVersion++;
    this.listeners.forEach((listener) => listener());
  }
}

// Export singleton instance
export const conversationStore = new ConversationStore();
