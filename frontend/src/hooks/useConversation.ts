/**
 * React hook for subscribing to a specific conversation's state
 * Uses useSyncExternalStore for efficient React integration
 */

import { useSyncExternalStore, useCallback, useEffect, useRef, useState } from 'react';
import { conversationStore, type ConversationState } from '../store/conversationStore';
import { webSocketManager } from '../services/WebSocketManager';
import { persistentWebSocket, type PersistentEvent } from '../services/PersistentWebSocket';
import {
  fetchConversation,
  fetchConversationLoadedSkills,
  fetchConversationTail,
  ApiClientError,
} from '../api/client';
import type { MessageContent, SubAgentToolCallInfo, SubAgentFinishedInfo, ToolResultMessage, SubAgentToolUseMessage, SubAgentToolResultMessage, StatsMessage, ComposerAttachmentRef, ActionRequestMessage as ActionRequestMessageType, PendingWaitInfo, ExpensiveResumeInfo, SubagentRunInfo } from '../api/types';

/**
 * Reconstruct the subAgentToolCalls map from persisted tool_result messages.
 *
 * When a conversation is loaded from the backend, tool_result messages for
 * agent_task / agent_task_parallel may contain a `sub_agent_tool_calls` array
 * of persisted sub-agent events. This function processes those arrays and
 * populates the conversationStore so the UI can render the sub-agent tree.
 */
function hydrateSubAgentToolCalls(conversationId: string, messages: MessageContent[]): void {
  for (const msg of messages) {
    if (msg.type !== 'tool_result') continue;
    const toolResult = msg as ToolResultMessage;
    const events = toolResult.sub_agent_tool_calls;
    if (!events || events.length === 0) continue;

    const parentToolId = toolResult.tool_id;

    for (const event of events) {
      if (event.type === 'sub_agent_tool_use') {
        const toolUseMsg: SubAgentToolUseMessage = {
          type: 'sub_agent_tool_use',
          parent_tool_id: event.parent_tool_id,
          agent_name: event.agent_name,
          tool_name: event.tool_name || 'unknown',
          tool_input: event.tool_input || {},
          tool_id: event.tool_id || '',
          intent_message: event.intent_message || '',
          nested_agent_id: event.nested_agent_id,
          nested_agent_name: event.nested_agent_name,
          nested_agent_model: event.nested_agent_model,
          nested_parent_id: event.nested_parent_id,
        };
        conversationStore.addSubAgentToolUse(conversationId, parentToolId, {
          agentName: toolUseMsg.agent_name,
          toolUse: toolUseMsg,
        });
      } else if (event.type === 'sub_agent_tool_result') {
        const toolResultMsg: SubAgentToolResultMessage = {
          type: 'sub_agent_tool_result',
          parent_tool_id: event.parent_tool_id,
          agent_name: event.agent_name,
          tool_id: event.tool_id || '',
          tool_output: event.tool_output || '',
          nested_agent_id: event.nested_agent_id,
          nested_agent_status: event.nested_agent_status,
          nested_parent_id: event.nested_parent_id,
        };
        conversationStore.updateSubAgentToolResult(
          conversationId,
          parentToolId,
          toolResultMsg.tool_id,
          toolResultMsg,
        );
      } else if (event.type === 'sub_agent_finished') {
        // Reloaded conversations carry the per-agent terminal state so
        // the COMPLETED / ERRORED badge survives a page reload. A 2nd-level
        // agent's finished event keys under the nested-agent node id so its
        // inner section resolves on reload, mirroring the live path.
        const status: 'success' | 'error' = event.status === 'error' ? 'error' : 'success';
        conversationStore.markSubAgentFinished(
          conversationId,
          event.nested_parent_id || parentToolId,
          event.agent_name,
          status,
          event.error,
        );
      }
    }
  }
}

/**
 * Lock the composer for any open ``action_request`` message that surfaces
 * via the tail-fetch path mid-turn. ``wait_handle_id`` is set when the
 * action request was created with a corresponding ``tool_wait_handles``
 * row -- the only situation where the agent is actually blocked.
 *
 * The resolved-event path clears entries by handle id, so this seeds the
 * store with rows the GET payload may not have included (e.g. the wait
 * row was created after the conversation was first loaded).
 */
function deriveActionRequestPendingWaits(messages: MessageContent[]): PendingWaitInfo[] {
  const out: PendingWaitInfo[] = [];
  for (const m of messages) {
    if (m.type !== 'action_request') continue;
    const ar = m as ActionRequestMessageType;
    if (ar.status !== 'open') continue;
    if (!ar.wait_handle_id) continue;
    out.push({
      id: ar.wait_handle_id,
      kind: 'action_request',
      correlation_id: ar.request_id != null ? String(ar.request_id) : null,
      created_at: ar.timestamp ?? null,
    });
  }
  return out;
}

/**
 * Merge newly-derived pending waits into the store, deduping by handle id.
 * Used after a tail-fetch or catchup insert so a freshly-arrived
 * ``action_request`` message locks the composer without a round trip back
 * to the conversation detail endpoint.
 */
function mergePendingWaits(
  conversationId: string,
  newWaits: PendingWaitInfo[],
): void {
  if (newWaits.length === 0) return;
  const current = conversationStore.getConversationSnapshot(conversationId).pendingWaitHandles;
  const seen = new Set(current.map((h) => h.id));
  const additions = newWaits.filter((h) => !seen.has(h.id));
  if (additions.length === 0) return;
  conversationStore.setPendingWaitHandles(
    conversationId,
    [...current, ...additions],
  );
}

/**
 * Hydrate context usage from persisted stats messages.
 *
 * When a conversation is loaded from the backend, scan the messages array
 * backward for the last StatsMessage and extract context_tokens /
 * max_context_tokens from it. This ensures the context indicator shows up
 * when loading an existing conversation, not just during live streaming.
 */
function hydrateContextUsage(conversationId: string, messages: MessageContent[]): void {
  for (let i = messages.length - 1; i >= 0; i--) {
    const msg = messages[i];
    if (msg.type === 'stats') {
      const statsMsg = msg as StatsMessage;
      if (statsMsg.stats.context_tokens != null && statsMsg.stats.max_context_tokens != null) {
        conversationStore.setContextUsage(
          conversationId,
          statsMsg.stats.context_tokens,
          statsMsg.stats.max_context_tokens,
        );
        return;
      }
    }
  }
}

interface UseConversationOptions {
  onLoadedSkills?: (conversationId: string, skillIds: string[]) => void;
  onModelLoaded?: (conversationId: string, model: string) => void;
  onProjectIdLoaded?: (conversationId: string, projectId: string) => void;
  onOriginLoaded?: (conversationId: string, origin: 'web' | 'slack' | 'user_subagent' | 'inference_api') => void;
}

interface UseConversationResult {
  messages: MessageContent[];
  isStreaming: boolean;
  partialResponse: string;
  streamingMessages: MessageContent[];
  error: string | null;
  stacktrace: string | undefined;
  isLoaded: boolean;
  isLoading: boolean;
  sendMessage: (message: string, model?: string, guideId?: string, skillIds?: string[], attachments?: ComposerAttachmentRef[], flags?: string[], attachedFilenames?: string[]) => void;
  addUserMessage: (content: string) => void;
  subAgentToolCalls: Map<string, SubAgentToolCallInfo[]>;
  subAgentReturned: Map<string, Map<string, SubAgentFinishedInfo>>;
  contextTokens: number | null;
  maxContextTokens: number | null;
  origin: 'web' | 'slack' | 'user_subagent' | 'inference_api';
  /** Caller identity + status of the cross-user subagent run, when
   *  origin === 'user_subagent'; null otherwise. Drives the read-only
   *  composer notice. */
  subagentRun: SubagentRunInfo | null;
  slackChannelId: string | null;
  slackThreadTs: string | null;
  slackTeamId: string | null;
  /** Raw model value from the conversation record. `null` means "no model
   *  set on the conversation" (server default applies at run time). Distinct
   *  from the in-context selected model which always falls back to a local
   *  default. */
  conversationModel: string | null;
  /** Persisted per-conversation flags (opt-in behaviors locked at the start).
   *  Empty before the first message; drives the read-only composer label. */
  conversationFlags: string[];
  /** User-set display name (null = auto title), with an optimistic setter
   *  for the chat header's Rename action. */
  customName: string | null;
  setCustomName: (name: string | null) => void;
  archived: boolean;
  setArchived: (archived: boolean) => void;
  /** True when the agent has at least one pending wait handle for this
   *  conversation (action_request awaiting approval, or slack_reply
   *  awaiting a Slack DM). Used by the composer to disable input even
   *  when the originating tab isn't the one driving the turn. */
  hasPendingWait: boolean;
  /** Discriminator for the placeholder copy: ``'action_request'`` /
   *  ``'slack_reply'`` / ``null`` if no wait is pending. When multiple
   *  waits exist, the first one (oldest by insertion) wins. */
  pendingWaitKind: string | null;
  /** Number of pending wait handles (several cards from one parallel batch). */
  pendingWaitCount: number;
  /** Server verdict that resuming this conversation is expensive (long-idle,
   *  long-context, costly model); null when no warning applies. */
  expensiveResume: ExpensiveResumeInfo | null;
  /** True while the composer should be blocked behind the expensive-resume
   *  warning card (verdict present and not yet acknowledged). */
  expensiveResumeBlocked: boolean;
  /** Unlock the composer after the user clicks "Continue anyway"; later
   *  sends carry the ``expensive_resume_acknowledged`` flag. */
  acknowledgeExpensiveResume: () => void;
  /** Drop the warning entirely (no acknowledgement flag on later sends);
   *  used after a successful server-side compaction. */
  clearExpensiveResume: () => void;
}

/**
 * Hook to subscribe to and interact with a specific conversation
 */
/**
 * Random id for an outbound send. ``crypto.randomUUID`` is only defined in
 * secure contexts, and local instances are served over plain http on a raw
 * IP, so fall back to a time + random string there.
 */
function newClientSendId(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID();
  }
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

export function useConversation(conversationId: string, options?: UseConversationOptions): UseConversationResult {
  const prevConversationIdRef = useRef<string | null>(null);
  const [origin, setOrigin] = useState<'web' | 'slack' | 'user_subagent' | 'inference_api'>('web');
  const [subagentRun, setSubagentRun] = useState<SubagentRunInfo | null>(null);
  const [slackChannelId, setSlackChannelId] = useState<string | null>(null);
  const [slackThreadTs, setSlackThreadTs] = useState<string | null>(null);
  const [slackTeamId, setSlackTeamId] = useState<string | null>(null);
  const [conversationModel, setConversationModel] = useState<string | null>(null);
  const [conversationFlags, setConversationFlags] = useState<string[]>([]);
  // Header title unit state: hydrated from GET /conversations/{id}, then
  // updated optimistically by the header's Rename / Archive actions.
  const [customName, setCustomName] = useState<string | null>(null);
  const [archived, setArchived] = useState(false);

  // Subscribe to the store using useSyncExternalStore
  const subscribe = useCallback(
    (callback: () => void) => {
      return conversationStore.subscribe(callback);
    },
    []
  );

  // Return the per-conversation state reference as the snapshot.
  // This reference only changes when this specific conversation is updated,
  // so the component won't re-render when unrelated conversations change.
  const getSnapshot = useCallback(() => {
    return conversationStore.getConversationSnapshot(conversationId);
  }, [conversationId]);

  // This will re-render the component only when this conversation's state changes
  const state: ConversationState = useSyncExternalStore(subscribe, getSnapshot);

  // Load conversation from API if not loaded
  useEffect(() => {
    // Only load if:
    // 1. Conversation ID changed, OR
    // 2. Not loaded and not currently loading
    const isNewConversation = prevConversationIdRef.current !== conversationId;
    prevConversationIdRef.current = conversationId;

    if (!isNewConversation && (state.isLoaded || state.isLoading)) {
      return;
    }

    // Already-seeded fast path: the sidebar's ``handleNewChat`` (or another
    // tab's optimistic write) primed the store with empty messages and a
    // last_seq of 0 before navigating, so the empty composer can paint on
    // the first commit. We still fire ``fetchConversation`` below as a
    // background reconcile, but skip the ``setLoading(true)`` toggle so
    // ``ChatPanel`` doesn't fall back to the "Loading conversation..."
    // placeholder while the safety-net fetch is in flight.
    const alreadySeeded = state.isLoaded && state.messages.length === 0;

    async function loadConversation() {
      try {
        if (!alreadySeeded) {
          conversationStore.setLoading(conversationId, true);
        }
        conversationStore.clearError(conversationId);

        const response = await fetchConversation(conversationId);

        // Convert messages to MessageContent format
        const convertedMessages = response.messages.map((msg) => ({
          ...msg,
          type: (msg as unknown as { type?: string }).type || 'text',
        })) as unknown as MessageContent[];

        conversationStore.setMessages(conversationId, convertedMessages);

        // Seed pending wait handles before any later WS events can fire so
        // a ``wait_handle_resolved`` race during load still finds the row
        // in the store to remove.
        conversationStore.setPendingWaitHandles(
          conversationId,
          response.pending_wait_handles ?? [],
        );

        // Seed the expensive-resume verdict so the composer can block
        // behind the warning card on load.
        conversationStore.setExpensiveResume(
          conversationId,
          response.expensive_resume ?? null,
        );

        // Seed the persistent-WS last-seq cache from the server's high-water
        // mark so the next subscribe issues an up_to_date / catchup rather
        // than a fresh subscribe-from-zero.
        if (typeof response.last_message_seq === 'number') {
          persistentWebSocket.setLastSeq(conversationId, response.last_message_seq);
        }

        // Hydrate sub-agent tool call data from persisted messages.
        // tool_result messages for agent_task / agent_task_parallel may carry
        // a sub_agent_tool_calls array that was saved alongside them.
        hydrateSubAgentToolCalls(conversationId, convertedMessages);

        // Hydrate context usage from the last stats message so the
        // context indicator works for previously loaded conversations.
        hydrateContextUsage(conversationId, convertedMessages);

        // Hydrate the per-conversation model from the server response.
        // `response.model` is absent/null for conversations created with no
        // explicit model (e.g. Slack DMs where the user never set a default).
        setConversationModel(response.model ?? null);
        if (options?.onModelLoaded && response.model) {
          options.onModelLoaded(conversationId, response.model);
        }

        // Hydrate persisted per-conversation flags so the composer can render
        // the read-only "N flags enabled" label after the first message.
        setConversationFlags(response.flags ?? []);
        setCustomName(response.custom_name ?? null);
        setArchived(Boolean(response.archived));

        // Report the project_id back to the caller (for URL correction)
        if (options?.onProjectIdLoaded && response.project_id) {
          options.onProjectIdLoaded(conversationId, response.project_id);
        }

        // Track origin so the composer can be locked for Slack-driven,
        // cross-user subagent, and inference API conversations.
        const nextOrigin: 'web' | 'slack' | 'user_subagent' | 'inference_api' =
          response.origin === 'slack'
            || response.origin === 'user_subagent'
            || response.origin === 'inference_api'
            ? response.origin
            : 'web';
        setOrigin(nextOrigin);
        setSubagentRun(response.subagent_run ?? null);
        setSlackChannelId(response.slack_channel_id ?? null);
        setSlackThreadTs(response.slack_thread_ts ?? null);
        setSlackTeamId(response.slack_team_id ?? null);
        if (options?.onOriginLoaded) {
          options.onOriginLoaded(conversationId, nextOrigin);
        }

        // Hydrate loaded skills from the server
        if (options?.onLoadedSkills) {
          try {
            const loadedSkillsResp = await fetchConversationLoadedSkills(conversationId);
            options.onLoadedSkills(conversationId, loadedSkillsResp.skill_ids);
          } catch {
            // Non-fatal: loaded skills state will just be empty
          }
        }
      } catch (err) {
        if (err instanceof ApiClientError) {
          conversationStore.setError(conversationId, err.message);
        } else {
          conversationStore.setError(conversationId, 'Failed to load conversation');
        }
        conversationStore.setLoading(conversationId, false);
      }
    }

    loadConversation();
  }, [conversationId, state.isLoaded, state.isLoading]);

  // Persistent WS: subscribe for the active conversation lifetime, watch
  // for ``message_appended`` events, and incrementally fetch the new tail
  // (or full refetch on ``resync``). The subscribe -> server -> bus path
  // handles dedup; the persistent socket forwards events at most once per
  // connection. The singleton owns the periodic refresh timer that keeps
  // the server-side TTL alive while this hook holds a subscription, so we
  // do not unsubscribe on cleanup -- a quick tab switch back finds the
  // server-side channel still live and any in-flight ``text_delta`` events
  // already buffered in WebSocketManager.
  useEffect(() => {
    if (!conversationId) return;
    persistentWebSocket.subscribe(conversationId);

    const unsubscribe = persistentWebSocket.onConversationEvent(
      conversationId,
      (event: PersistentEvent) => {
        const type = event.type as string | undefined;
        if (type === 'message_appended') {
          // Fire-and-forget tail fetch. The server-side seq is already
          // tracked in the singleton so a follow-up subscribe (e.g. after
          // a tab refresh) starts in catchup mode.
          const lastKnown = persistentWebSocket.getLastSeq(conversationId);
          fetchConversationTail(conversationId, lastKnown - 1)
            .then((resp) => {
              const newMessages = (resp.messages || []).map((m) => ({
                ...(m as Record<string, unknown>),
                type: ((m as Record<string, unknown>).type as string | undefined) || 'text',
              })) as unknown as MessageContent[];
              if (newMessages.length === 0) return;

              // Two tail-fetches racing (seq=N and seq=N+1) can resolve in
              // either order; insert by seq so the rendered list stays in
              // disk order regardless of which fetch lands first.
              const toInsert: MessageContent[] = [];
              for (const m of newMessages) {
                const role = (m as unknown as { role?: string }).role;
                if (role === 'user' && conversationStore.reconcileOptimisticUserMessage(conversationId, m)) {
                  continue;
                }
                toInsert.push(m);
              }
              conversationStore.insertMessagesBySeq(conversationId, toInsert);

              if (typeof resp.last_message_seq === 'number') {
                persistentWebSocket.setLastSeq(conversationId, resp.last_message_seq);
              }

              // Sub-agent tree may have grown; re-hydrate from the
              // freshly-fetched messages.
              hydrateSubAgentToolCalls(conversationId, newMessages);
              hydrateContextUsage(conversationId, newMessages);
              mergePendingWaits(
                conversationId,
                deriveActionRequestPendingWaits(newMessages),
              );
            })
            .catch(() => {
              // Silently ignore tail-fetch errors; the next event or a
              // page reload recovers the missing tail.
            });
        } else if (type === 'subscribed') {
          // Reconcile the streaming/stop state with the server's
          // ``run_active`` verdict first: a run that ended (or started)
          // while this tab's socket was down never delivered its
          // transient lifecycle envelope, and this ack is the only
          // signal that replaces it.
          webSocketManager.syncRunState(conversationId, event.run_active);

          // Server may answer ``catchup`` with embedded message bodies.
          // Apply them directly without a refetch.
          const mode = event.mode as string | undefined;
          if (mode === 'catchup' && Array.isArray(event.messages)) {
            const fresh = (event.messages as Array<Record<string, unknown>>).map((m) => ({
              ...m,
              type: (m.type as string | undefined) || 'text',
            })) as unknown as MessageContent[];
            const toInsert: MessageContent[] = [];
            for (const m of fresh) {
              // Same optimistic-reconcile path as the message_appended branch
              // -- a catchup after reconnect can deliver the user message
              // right alongside the still-unstamped optimistic bubble.
              const role = (m as unknown as { role?: string }).role;
              if (role === 'user' && conversationStore.reconcileOptimisticUserMessage(conversationId, m)) {
                continue;
              }
              toInsert.push(m);
            }
            conversationStore.insertMessagesBySeq(conversationId, toInsert);
            hydrateSubAgentToolCalls(conversationId, fresh);
            hydrateContextUsage(conversationId, fresh);
            mergePendingWaits(
              conversationId,
              deriveActionRequestPendingWaits(fresh),
            );
          } else if (mode === 'resync') {
            // Force a fresh hydrate of the conversation -- the server
            // can't serve a catchup window from the buffer.
            void (async () => {
              try {
                const full = await fetchConversation(conversationId);
                const converted = full.messages.map((msg) => ({
                  ...msg,
                  type: (msg as unknown as { type?: string }).type || 'text',
                })) as unknown as MessageContent[];
                conversationStore.setMessages(conversationId, converted);
                if (typeof full.last_message_seq === 'number') {
                  persistentWebSocket.setLastSeq(conversationId, full.last_message_seq);
                }
                hydrateSubAgentToolCalls(conversationId, converted);
                hydrateContextUsage(conversationId, converted);
                conversationStore.setPendingWaitHandles(
                  conversationId,
                  full.pending_wait_handles ?? [],
                );
                conversationStore.setExpensiveResume(
                  conversationId,
                  full.expensive_resume ?? null,
                );
              } catch {
                // Silently ignore; next event or reload recovers.
              }
            })();
          }
        } else if (type === 'resume_started') {
          // A headless wait-handle resume (the continuation after an
          // action-request Approve / Revise / Deny) started streaming on
          // this conversation. Enter the same streaming state as a local
          // send so the composer shows the stop button instead of
          // accepting a doomed second send; the resume's
          // ``send_message_finished`` ends it through the normal path.
          webSocketManager.attachToResumeStream(conversationId);
        } else if (type === 'resync') {
          // Server-emitted forced resync (post-subscribe overflow). Same
          // recovery path as ``subscribed`` with mode=resync.
          void (async () => {
            try {
              const full = await fetchConversation(conversationId);
              const converted = full.messages.map((msg) => ({
                ...msg,
                type: (msg as unknown as { type?: string }).type || 'text',
              })) as unknown as MessageContent[];
              conversationStore.setMessages(conversationId, converted);
              if (typeof full.last_message_seq === 'number') {
                persistentWebSocket.setLastSeq(conversationId, full.last_message_seq);
              }
              hydrateSubAgentToolCalls(conversationId, converted);
              hydrateContextUsage(conversationId, converted);
              conversationStore.setPendingWaitHandles(
                conversationId,
                full.pending_wait_handles ?? [],
              );
            } catch {
              // ignore
            }
          })();
        }
      },
    );

    return () => {
      unsubscribe();
    };
  }, [conversationId]);

  // Listen for ``wait_handle_resolved`` per-user globals so the composer
  // unlocks the moment any tab (or REST resolve, or stop, or timeout)
  // closes a pending wait for this conversation. Distinct from the
  // per-conversation subscription effect above because the resolved
  // envelope routes through the global channel.
  useEffect(() => {
    if (!conversationId) return;
    return persistentWebSocket.onGlobalEvent((event) => {
      if (event.type !== 'wait_handle_resolved') return;
      const eventConversationId = event.conversation_id as string | undefined;
      if (eventConversationId !== conversationId) return;
      const handleId = event.handle_id as string | undefined;
      if (!handleId) return;
      conversationStore.removePendingWaitHandle(conversationId, handleId);
    });
  }, [conversationId]);

  // Keep the header title unit in sync when the model names the chat via
  // the ``set_conversation_name`` tool (server emits ``conversation_updated``
  // with the new custom_name). The sidebar listens to the same callback;
  // without this the header kept showing the first-message title until a
  // reload. An empty name means the custom name was cleared.
  useEffect(() => {
    if (!conversationId) return;
    return webSocketManager.onConversationRenamed((renamedId, name) => {
      if (renamedId !== conversationId) return;
      setCustomName(name || null);
    });
  }, [conversationId]);

  // Send message function
  const sendMessage = useCallback(
    (
      message: string,
      model?: string,
      guideId?: string,
      skillIds?: string[],
      attachments?: ComposerAttachmentRef[],
      flags?: string[],
      attachedFilenames?: string[],
    ) => {
      // Allow attachment-only sends (text may be empty when images are attached).
      if (!message.trim() && (!attachments || attachments.length === 0)) return;

      // Check if already streaming in this conversation
      if (conversationStore.isStreaming(conversationId)) {
        console.warn('Already streaming in this conversation');
        return;
      }

      // Belt-and-suspenders: the composer is disabled while a wait handle
      // is pending, but a programmatic send (e.g. auto-routine kick) must
      // also respect it -- the server would reject the turn anyway via
      // the in-flight resume bucket.
      if (conversationStore.hasPendingWaitHandle(conversationId)) {
        console.warn('Pending wait handle in this conversation');
        return;
      }

      // Same belt-and-suspenders for the expensive-resume block: the
      // composer is disabled behind the warning card, and the server-side
      // gate would reject an unacknowledged send anyway.
      if (conversationStore.isExpensiveResumeBlocked(conversationId)) {
        console.warn('Expensive-resume warning not acknowledged in this conversation');
        return;
      }

      // Optimistically add user message to the store. Tag it ``optimistic``
      // so the persistent-WS ``message_appended`` round-trip can reconcile
      // it with the server-stamped row instead of appending a duplicate.
      // ``client_send_id`` ties the bubble to the server's
      // ``send_message_accepted`` receipt and to the failed-send Retry /
      // Discard actions (see WebSocketManager.sendMessage).
      const clientSendId = newClientSendId();
      const userMessage: MessageContent = {
        role: 'user',
        content: message,
        timestamp: new Date().toISOString(),
        optimistic: true,
        client_send_id: clientSendId,
        ...(attachments && attachments.length > 0 && { attachments }),
      };
      conversationStore.addMessage(conversationId, userMessage);

      // Send via WebSocket manager (cookie auth - no API key needed)
      webSocketManager.sendMessage(conversationId, message, model, guideId, skillIds, attachments, flags, attachedFilenames, clientSendId);
    },
    [conversationId]
  );

  // Add user message without sending (for when component needs to add message manually)
  const addUserMessage = useCallback(
    (content: string) => {
      // Same optimistic flag as ``sendMessage`` -- callers of this helper
      // expect a server round-trip to follow that will fill in the seq.
      const userMessage: MessageContent = {
        role: 'user',
        content,
        timestamp: new Date().toISOString(),
        optimistic: true,
      };
      conversationStore.addMessage(conversationId, userMessage);
    },
    [conversationId]
  );

  const acknowledgeExpensiveResume = useCallback(() => {
    conversationStore.acknowledgeExpensiveResume(conversationId);
  }, [conversationId]);

  // Clear the warning entirely (vs. acknowledge): used after a successful
  // server-side compaction, when the verdict no longer applies.
  const clearExpensiveResume = useCallback(() => {
    conversationStore.setExpensiveResume(conversationId, null);
  }, [conversationId]);

  const pendingWaitHandles = state.pendingWaitHandles;
  return {
    messages: state.messages,
    isStreaming: state.isStreaming,
    partialResponse: state.partialResponse,
    streamingMessages: state.streamingMessages,
    error: state.error,
    stacktrace: state.stacktrace,
    isLoaded: state.isLoaded,
    isLoading: state.isLoading,
    sendMessage,
    addUserMessage,
    subAgentToolCalls: state.subAgentToolCalls,
    subAgentReturned: state.subAgentReturned,
    contextTokens: state.contextTokens,
    maxContextTokens: state.maxContextTokens,
    origin,
    subagentRun,
    slackChannelId,
    slackThreadTs,
    slackTeamId,
    conversationModel,
    conversationFlags,
    customName,
    setCustomName,
    archived,
    setArchived,
    hasPendingWait: pendingWaitHandles.length > 0,
    pendingWaitKind: pendingWaitHandles.length > 0 ? pendingWaitHandles[0].kind : null,
    pendingWaitCount: pendingWaitHandles.length,
    expensiveResume: state.expensiveResume,
    expensiveResumeBlocked: state.expensiveResume !== null && !state.expensiveResumeAcknowledged,
    acknowledgeExpensiveResume,
    clearExpensiveResume,
  };
}
