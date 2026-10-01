/**
 * ChatPanel component for displaying and sending messages in a conversation
 */

import React, { useEffect, useLayoutEffect, useState, useRef, useCallback, useMemo } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import remarkBreaks from 'remark-breaks';
import rehypeHighlight from 'rehype-highlight';
import remarkMath from 'remark-math';
import rehypeKatex from 'rehype-katex';
import remarkMathCurrencyGuard from '../utils/remarkMathCurrencyGuard';
import { useNavigate } from 'react-router-dom';
import { useConversation } from '../hooks/useConversation';
import { useConversationModels } from '../contexts/ConversationModelsContext';
import { useConversationSkills } from '../contexts/ConversationSkillsContext';
import { useNavigationState } from '../contexts/NavigationContext';
import { webSocketManager } from '../services/WebSocketManager';
import { compactConversation, duplicateConversationWorkspace, fetchProject } from '../api/client';
import type { MessageContent, ToolUseMessage as ToolUseMessageType, ToolResultMessage as ToolResultMessageType, ComposerAttachmentRef } from '../api/types';
import { MessageContentRenderer, markdownComponents, MarkdownWorkspaceContext } from './Message';
import { ToolCallGroup } from './ToolCallGroup';
import type { ToolCallGroupItem } from './ToolCallGroup';
import { getProviderForModel } from '../constants/models';
import type { ModelVisibility } from '../constants/models';
import { Composer } from './Composer';
import { ConvertToProjectModal } from './ConvertToProjectModal';
import { ExpensiveResumeWarning } from './ExpensiveResumeWarning';
import { ConversationHeader } from './ConversationHeader';
import { FileViewerModal } from './FileViewerModal';
import { downloadFile as apiDownloadFile, uploadFiles } from '../api/fileApi';
import { buildSlackThreadUrl } from '../utils/slackLinks';
import { seedNewConversation } from '../utils/newConversation';
import './ChatPanel.css';

interface ChatPanelProps {
  conversationId: string;
  onConversationUpdate?: () => void;
  onProjectIdLoaded?: (conversationId: string, projectId: string) => void;
}

/**
 * Check if user is near the bottom of the scroll container
 */
function isNearBottom(container: HTMLElement, threshold: number = 100): boolean {
  const { scrollTop, scrollHeight, clientHeight } = container;
  return scrollHeight - scrollTop - clientHeight < threshold;
}

/**
 * Find the corresponding tool_result for a tool_use message by scanning forward.
 * Unlike the old version, this does NOT stop at other tool_use messages, since
 * consecutive tool calls may interleave tool_use/tool_result pairs.
 */
function findToolResultById(messages: MessageContent[], toolId: string, startIndex: number): ToolResultMessageType | undefined {
  for (let i = startIndex + 1; i < messages.length; i++) {
    const msg = messages[i];
    if (msg.type === 'tool_result' && msg.tool_id === toolId) {
      return msg as ToolResultMessageType;
    }
  }
  return undefined;
}

/**
 * Grouping types for consecutive tool call rendering
 */
interface ToolCallGroupType {
  kind: 'tool_group';
  items: ToolCallGroupItem[];
}

type GroupedMessage =
  | { kind: 'message'; message: MessageContent; index: number }
  | ToolCallGroupType;

/**
 * Groups consecutive tool_use messages into collapsible units.
 * Non-tool messages (text, error, stats, interrupted, action_request) break the group.
 * tool_result messages are skipped (they are paired with their tool_use via findToolResultById).
 */
function groupConsecutiveToolCalls(messages: MessageContent[]): GroupedMessage[] {
  const result: GroupedMessage[] = [];
  let currentGroup: ToolCallGroupType | null = null;

  for (let i = 0; i < messages.length; i++) {
    const msg = messages[i];

    // Skip tool_result messages -- they are paired with their tool_use
    if (msg.type === 'tool_result') {
      continue;
    }

    if (msg.type === 'tool_use') {
      const toolUse = msg as ToolUseMessageType;
      const toolResult = findToolResultById(messages, toolUse.tool_id, i);

      if (!currentGroup) {
        currentGroup = { kind: 'tool_group', items: [] };
      }
      currentGroup.items.push({ toolUse, toolResult });
    } else {
      // Non-tool message: flush any current group, then add the message
      if (currentGroup) {
        result.push(currentGroup);
        currentGroup = null;
      }
      result.push({ kind: 'message', message: msg, index: i });
    }
  }

  // Flush any trailing group
  if (currentGroup) {
    result.push(currentGroup);
  }

  return result;
}

/**
 * ChatPanel wrapped in React.memo so it only re-renders when its props change.
 * Since conversationId is the only meaningful prop, this prevents re-renders
 * caused by parent re-renders that don't change the active conversation.
 */
export const ChatPanel = React.memo(function ChatPanel({ conversationId, onProjectIdLoaded }: ChatPanelProps) {
  const [wasNearBottom, setWasNearBottom] = useState(true);
  // Workspace file open in the viewer modal: composer attachments and inline
  // markdown images share it, so only the two fields the modal needs are kept.
  const [attachmentViewer, setAttachmentViewer] = useState<{ workspace_path: string; filename: string } | null>(null);
  const [isConvertModalOpen, setIsConvertModalOpen] = useState(false);

  const navigate = useNavigate();

  const messagesEndRef = useRef<HTMLDivElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const prevPartialResponseRef = useRef('');

  // Get model state from context
  const {
    getModelForConversation, hydrateModelForConversation,
    lockConversationProvider, isProviderLocked,
    refreshDefaultModel, persistDefaultModel,
  } = useConversationModels();
  const {
    pendingRoutineMessage, setPendingRoutineMessage,
    pendingFirstMessage, setPendingFirstMessage,
    scrollToMessageIndex, setScrollToMessageIndex,
  } = useNavigationState();
  const { setQueuedSkillsForConversation, setLoadedSkillsForConversation } = useConversationSkills();
  const selectedModel = getModelForConversation(conversationId);

  // Callback to hydrate loaded skills when conversation is loaded from the server
  const handleLoadedSkills = useCallback((convId: string, skillIds: string[]) => {
    setLoadedSkillsForConversation(convId, skillIds);
  }, [setLoadedSkillsForConversation]);

  // Callback to hydrate the per-conversation model when loaded from the server
  const handleModelLoaded = useCallback((convId: string, model: string) => {
    hydrateModelForConversation(convId, model);
  }, [hydrateModelForConversation]);

  // Public-project detection: when the loaded conversation reveals a
  // project_id, fetch the project to learn its ``public`` flag. Public
  // conversations show a persistent sensitive-info warning banner above the
  // composer and hide its Skill button (the backend ignores skill_ids there
  // anyway).
  const [isPublicProject, setIsPublicProject] = useState(false);
  useEffect(() => {
    setIsPublicProject(false);
  }, [conversationId]);
  const handleProjectIdLoaded = useCallback((convId: string, projId: string) => {
    onProjectIdLoaded?.(convId, projId);
    fetchProject(projId)
      .then((p) => {
        if (convId === conversationId) setIsPublicProject(Boolean(p.public));
      })
      .catch(() => { /* non-fatal: fall back to the non-public composer */ });
  }, [onProjectIdLoaded, conversationId]);

  // Use the new conversation hook
  const {
    messages,
    isStreaming,
    partialResponse,
    streamingMessages,
    error,
    isLoaded,
    isLoading,
    sendMessage,
    subAgentToolCalls,
    subAgentReturned,
    contextTokens,
    maxContextTokens,
    origin,
    subagentRun,
    slackChannelId,
    slackThreadTs,
    slackTeamId,
    conversationModel,
    conversationFlags,
    hasPendingWait,
    pendingWaitKind,
    pendingWaitCount,
    expensiveResume,
    expensiveResumeBlocked,
    acknowledgeExpensiveResume,
    clearExpensiveResume,
    customName,
    setCustomName,
    archived,
    setArchived,
  } = useConversation(conversationId, { onLoadedSkills: handleLoadedSkills, onModelLoaded: handleModelLoaded, onProjectIdLoaded: handleProjectIdLoaded });

  // Slack-driven conversations are read-only in the web UI; the user is
  // conversing via Slack DMs and the model replies through the
  // send_slack_reply_and_get_response tool. Cross-user subagent
  // conversations are read-only too: the owner watches the headless run and
  // only interacts through the subagent_return approval card. Inference API
  // conversations are one-shot transcripts driven by POST /api/inference.
  const isReadOnly = origin === 'slack' || origin === 'user_subagent' || origin === 'inference_api';

  // Build a deep link to the driving Slack thread, when we have all three
  // identifiers. When default_team_id is missing we simply omit the link
  // rather than render a broken anchor.
  const slackThreadUrl = (origin === 'slack' && slackChannelId && slackThreadTs && slackTeamId)
    ? buildSlackThreadUrl(slackTeamId, slackChannelId, slackThreadTs)
    : null;

  // Read-only banner copy for the subagent case (replaces the Slack copy in
  // the composer's banner). Caller identity comes from the subagent_run
  // block on the GET response.
  const subagentCallerDisplay = subagentRun
    ? (subagentRun.caller_name
      ? `${subagentRun.caller_name} (${subagentRun.caller_email})`
      : subagentRun.caller_email)
    : 'another user';
  const readOnlyNotice = origin === 'user_subagent'
    ? `Subagent run by ${subagentCallerDisplay} — read-only. Interact via the approval card when it appears.`
    : origin === 'inference_api'
      ? 'Inference API run — a read-only transcript of a one-shot request made by an application with your API key.'
      : null;

  // (The composer's disabled state and placeholder copy now live in
  // <Composer>, derived from the isStreaming/isReadOnly/hasPendingWait props
  // we pass down below.)

  // Handle scroll to track if user is near bottom
  const handleScroll = useCallback(() => {
    if (containerRef.current) {
      setWasNearBottom(isNearBottom(containerRef.current));
    }
  }, []);

  // Scroll to bottom function
  const scrollToBottom = useCallback((behavior: ScrollBehavior = 'smooth') => {
    if (messagesEndRef.current) {
      messagesEndRef.current.scrollIntoView({ behavior, block: 'end' });
    }
  }, []);

  // Track scroll position when streaming starts. (The streaming-end
  // auto-focus of the textarea now lives in <Composer>, which owns the
  // textarea ref.)
  const prevStreamingRef = useRef(false);
  useEffect(() => {
    if (isStreaming && !prevStreamingRef.current) {
      // Streaming just started
      if (containerRef.current) {
        setWasNearBottom(isNearBottom(containerRef.current));
      }
    }
    prevStreamingRef.current = isStreaming;
  }, [isStreaming]);

  // Auto-scroll during streaming if user was near bottom
  useEffect(() => {
    if (isStreaming && wasNearBottom) {
      scrollToBottom('smooth');
    }
  }, [partialResponse, streamingMessages, isStreaming, wasNearBottom, scrollToBottom]);

  // Streaming -> finalized transition catch-up.
  //
  // When ``message_appended`` lands on the primary tab, the store does an
  // atomic swap: ``partialResponse`` clears AND a synthetic persisted text
  // entry is inserted at the event's seq. The streaming bubble unmounts and
  // a same-seq persisted bubble mounts in its place; the latter is taller
  // (full message header with timestamp, plus the trailing tail-fetch will
  // add a stats row shortly after) so layout grows by N px in a single
  // commit.
  //
  // The ``isStreaming && wasNearBottom`` streaming-effect can miss this:
  // smooth-scroll animations triggered by earlier text_deltas leave
  // scrollTop trailing scrollHeight, so ``handleScroll`` mid-animation can
  // observe distance > threshold and flip ``wasNearBottom`` to false. Once
  // false, neither the streaming effect nor the length-keyed effect below
  // fires, and the view stays anchored where the streaming bubble was --
  // i.e. above the new bottom, with the final lines clipped.
  //
  // Detect the transition explicitly (non-empty -> empty partialResponse)
  // and force the scroll, since by definition the user was tracking the
  // stream. useLayoutEffect runs after the DOM commit but before paint, so
  // the scroll picks up the new (taller) content height instead of the
  // pre-swap layout.
  useLayoutEffect(() => {
    const prev = prevPartialResponseRef.current;
    prevPartialResponseRef.current = partialResponse;
    if (prev && !partialResponse) {
      scrollToBottom('smooth');
    }
  }, [partialResponse, scrollToBottom]);

  // Follow the persisted-message tail when new messages arrive (e.g. tail-fetch
  // after a message_appended event lands AFTER the streaming bubble cleared).
  // Without this, the post-stream scroll position predates the new content and
  // the last message hangs below the viewport.
  useEffect(() => {
    if (isLoaded && !isLoading && wasNearBottom && messages.length > 0) {
      scrollToBottom('smooth');
    }
  }, [messages.length, isLoaded, isLoading, wasNearBottom, scrollToBottom]);

  // Scroll to bottom after loading conversation (instant)
  useEffect(() => {
    if (isLoaded && !isLoading) {
      setTimeout(() => scrollToBottom('instant'), 50);
    }
  }, [isLoaded, isLoading, conversationId, scrollToBottom]);

  // Lock provider when conversation already has messages (loaded from server)
  useEffect(() => {
    if (isLoaded && messages.length > 0 && !isProviderLocked(conversationId)) {
      lockConversationProvider(conversationId, getProviderForModel(selectedModel));
    }
  }, [isLoaded, messages.length, isProviderLocked, lockConversationProvider, conversationId, selectedModel]);

  // Scroll to a specific message when scrollToMessageIndex is set (from search navigation)
  useEffect(() => {
    if (scrollToMessageIndex !== null && isLoaded && containerRef.current) {
      // Small delay to ensure DOM is rendered
      const timer = setTimeout(() => {
        const el = containerRef.current?.querySelector(
          `[data-message-index="${scrollToMessageIndex}"]`
        );
        if (el) {
          el.scrollIntoView({ behavior: 'smooth', block: 'center' });
          // Brief highlight animation
          el.classList.add('message-search-highlight');
          setTimeout(() => el.classList.remove('message-search-highlight'), 2000);
        }
        setScrollToMessageIndex(null);
      }, 150);
      return () => clearTimeout(timer);
    }
  }, [scrollToMessageIndex, isLoaded, conversationId, setScrollToMessageIndex]);

  // Auto-send pending routine message when this conversation loads
  // This effect runs when a routine is invoked from the sidebar: the sidebar creates
  // a new conversation, sets pendingRoutineMessage, and navigates here. Once the
  // conversation is loaded and empty (fresh), we auto-send the routine's prompt.
  useEffect(() => {
    if (
      pendingRoutineMessage &&
      pendingRoutineMessage.conversationId === conversationId &&
      isLoaded &&
      !isStreaming &&
      messages.length === 0  // Only send if conversation is fresh (no messages yet)
    ) {
      const { prompt, guideId } = pendingRoutineMessage;
      setPendingRoutineMessage(null);  // Clear immediately to prevent re-send

      // Send the routine prompt, carrying the routine's guide override (if
      // any) -- the only remaining path that sends a guide_id.
      const model = getModelForConversation(conversationId);
      sendMessage(prompt, model, guideId ?? undefined);

      // Lock the provider after first message
      lockConversationProvider(conversationId, getProviderForModel(model));
    }
  }, [pendingRoutineMessage, conversationId, isLoaded, isStreaming, messages.length,
      setPendingRoutineMessage, getModelForConversation,
      sendMessage, lockConversationProvider]);

  // Auto-send pending first message when this conversation loads.
  //
  // Mirrors the routine effect above, but for the root home-screen composer
  // flow (see HomeComposer.tsx): the home screen creates a fresh conversation,
  // stashes the typed prompt + the home-chosen model/skills/flags in
  // ``pendingFirstMessage``, then navigates here. Once the conversation is
  // loaded and empty, we perform the real send carrying those selections, and
  // lock the provider exactly as ``handleSendMessage`` would on a
  // normal first send. Model and skills are carried explicitly in the field
  // (rather than re-read from the context maps) to avoid any race with the
  // map writes the home flow performs before navigating.
  useEffect(() => {
    if (
      pendingFirstMessage &&
      pendingFirstMessage.conversationId === conversationId &&
      isLoaded &&
      !isStreaming &&
      messages.length === 0
    ) {
      const { prompt, model, skillIds, flags, isPublicProject: startedPublic, attachedFilenames, attachments } = pendingFirstMessage;
      setPendingFirstMessage(null);  // Clear immediately to prevent re-send

      const effectiveModel = model || getModelForConversation(conversationId);
      const skillIdsToSend = skillIds && skillIds.length > 0 ? skillIds : undefined;
      const flagsToSend = flags && flags.length > 0 ? flags : undefined;

      // Make sure the queued skills are also recorded on this conversation so
      // the composer badge / loaded-skills set is consistent after the send.
      if (skillIdsToSend) {
        setQueuedSkillsForConversation(conversationId, skillIdsToSend);
      }

      // Persist the per-user last-used model for the chat's context (public
      // project vs private, as known by the home composer at send time --
      // this panel's own project fetch may not have resolved yet) on the
      // first send of this new chat (home-originated flow). This is one of
      // the two first-send choke points; selecting a model without sending
      // never persists.
      persistDefaultModel(effectiveModel, startedPublic ? 'public' : 'private');

      // Files and pasted images were already uploaded into the workspace by
      // HomeComposer before navigation; here we only forward the file names
      // (first-turn metadata) and the image refs (multimodal attachments).
      // No upload happens in this effect.
      const attachedFilenamesToSend = attachedFilenames && attachedFilenames.length > 0
        ? attachedFilenames
        : undefined;
      const attachmentsToSend = attachments && attachments.length > 0 ? attachments : undefined;
      sendMessage(prompt, effectiveModel, undefined, skillIdsToSend, attachmentsToSend, flagsToSend, attachedFilenamesToSend);

      // Lock the provider after the first message.
      lockConversationProvider(conversationId, getProviderForModel(effectiveModel));
    }
  }, [pendingFirstMessage, conversationId, isLoaded, isStreaming, messages.length,
      setPendingFirstMessage,
      getModelForConversation, setQueuedSkillsForConversation, sendMessage,
      lockConversationProvider, persistDefaultModel]);

  // Always-fresh fetch on a new empty per-conversation composer mount.
  //
  // When a conversation loads with no messages, no per-conversation model
  // override (server-sourced ``conversationModel``), and no pending home-flow
  // first message, this composer is a fresh new-chat surface. Re-read the
  // per-user last-used model for the conversation's context -- public
  // project or private -- from the server so it reflects the latest value
  // across tabs (mirrors HomeComposer), and seed it as this conversation's
  // in-memory selection so the composer shows it. The public flag arrives
  // asynchronously (project fetch), so the refresh re-runs when it flips and
  // a resolution for the previous context is discarded. Guarded so it does
  // not re-fetch once a message exists, an override is present, or the home
  // auto-send is queued.
  const modelVisibility: ModelVisibility = isPublicProject ? 'public' : 'private';
  useEffect(() => {
    if (
      !isLoaded ||
      messages.length > 0 ||
      conversationModel ||
      (pendingFirstMessage && pendingFirstMessage.conversationId === conversationId)
    ) {
      return;
    }
    let cancelled = false;
    refreshDefaultModel(modelVisibility).then((resolved) => {
      if (!cancelled) hydrateModelForConversation(conversationId, resolved);
    });
    return () => {
      cancelled = true;
    };
    // Only the load state, the context and the conversation identity should
    // trigger a re-fetch, not every message/pending-first-message change.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isLoaded, conversationModel, conversationId, modelVisibility, refreshDefaultModel, hydrateModelForConversation]);

  // Wrap the live send path to persist the per-user default on the FIRST send
  // of a chat started directly from this composer (Sidebar "New Chat" then
  // typing -- not the home flow, which persists in the pendingFirstMessage
  // effect above). Fires exactly once per new conversation's first message;
  // subsequent sends do not persist.
  const handleComposerSend = useCallback(
    (
      text: string,
      model: string,
      skillIds?: string[],
      attachments?: ComposerAttachmentRef[],
      flags?: string[],
      files?: File[],
    ) => {
      if (messages.length === 0) {
        persistDefaultModel(model, modelVisibility);
      }
      // Generic file attachments: the conversation already exists in the live
      // chat, so upload them to the workspace root first, then send carrying
      // the uploaded names so this turn's metadata can list them. No files ->
      // straight send (the common path).
      if (files && files.length > 0) {
        uploadFiles(conversationId, files, '')
          .then((resp) => {
            const attachedFilenames = resp.uploadedFiles.map((f) => f.name);
            sendMessage(text, model, undefined, skillIds, attachments, flags, attachedFilenames);
          })
          .catch(() => {
            // Fall back to sending without the attached-filename hint rather
            // than dropping the message; the files just weren't uploaded.
            sendMessage(text, model, undefined, skillIds, attachments, flags);
          });
        return;
      }
      sendMessage(text, model, undefined, skillIds, attachments, flags);
    },
    [conversationId, messages.length, modelVisibility, persistDefaultModel, sendMessage],
  );

  // Handle stop/interrupt streaming
  const handleStopMessage = useCallback(() => {
    if (isStreaming) {
      webSocketManager.stopStreaming(conversationId);
    }
  }, [isStreaming, conversationId]);

  // ---- Expensive-resume warning actions ----

  // Duplicate Workspace: fresh chat seeded with a copy of this conversation's
  // workspace files (same flow as the Sidebar menu action). Errors propagate
  // to the warning card, which surfaces them inline.
  const handleExpensiveDuplicateWorkspace = useCallback(async () => {
    const response = await duplicateConversationWorkspace(conversationId);
    seedNewConversation(response.id, setLoadedSkillsForConversation);
    navigate(`/chats/${response.id}`);
  }, [conversationId, setLoadedSkillsForConversation, navigate]);

  // Compact the history server-side, then drop the warning: the verdict no
  // longer applies to the shrunken history. The "compaction" marker message
  // arrives on its own via the persistent WS (message_appended).
  const handleExpensiveCompact = useCallback(async () => {
    await compactConversation(conversationId);
    clearExpensiveResume();
  }, [conversationId, clearExpensiveResume]);

  const handleOpenConvertModal = useCallback(() => {
    setIsConvertModalOpen(true);
  }, []);

  const handleConvertedToProject = useCallback((projectId: string, convId: string) => {
    setIsConvertModalOpen(false);
    navigate(`/projects/${projectId}/${convId}`);
  }, [navigate]);

  // Prefill for the convert modal: same first-user-message slicing rule the
  // sidebar list titles use.
  const conversationTitle = (() => {
    for (const m of messages) {
      if ('role' in m && m.role === 'user' && typeof (m as { content?: unknown }).content === 'string') {
        const content = ((m as { content: string }).content).trim();
        if (content) return content.slice(0, 50);
      }
    }
    return 'New Chat';
  })();

  // The live send path. The shared <Composer> owns the textarea, attachment
  // upload, model/skills/flags selection and clears its own input; here we
  // just dispatch the real send and scroll the message list to the bottom.
  const handleAfterSend = useCallback(() => {
    setTimeout(() => scrollToBottom('smooth'), 50);
  }, [scrollToBottom]);

  const handleOpenAttachment = useCallback((att: ComposerAttachmentRef) => {
    setAttachmentViewer({ workspace_path: att.workspace_path, filename: att.filename });
  }, []);

  const handleOpenMarkdownImage = useCallback((workspacePath: string, filename: string) => {
    setAttachmentViewer({ workspace_path: workspacePath, filename });
  }, []);

  // Lets the shared markdown renderer resolve `![alt](path.png)` image refs
  // against this conversation's workspace (see MarkdownImage in Message.tsx).
  const markdownWorkspaceCtx = useMemo(
    () => ({ conversationId, onOpenImage: handleOpenMarkdownImage }),
    [conversationId, handleOpenMarkdownImage]
  );

  const handleCloseAttachmentViewer = useCallback(() => {
    setAttachmentViewer(null);
  }, []);

  const handleDownloadAttachment = useCallback(async () => {
    if (!attachmentViewer) return;
    try {
      const { url, filename } = await apiDownloadFile(conversationId, attachmentViewer.workspace_path);
      const a = document.createElement('a');
      a.href = url;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      setTimeout(() => URL.revokeObjectURL(url), 0);
    } catch {
      // Swallow; the modal stays open and the user can retry.
    }
  }, [attachmentViewer, conversationId]);

  // Render messages with consecutive tool calls grouped into collapsible units
  const renderGroupedMessages = (allMessages: MessageContent[]) => {
    // Pre-scan: build a map from message index to model (from the next stats message in the same assistant turn)
    const modelByIndex = new Map<number, string>();
    for (let i = 0; i < allMessages.length; i++) {
      const msg = allMessages[i];
      if ((msg.type === 'text' || (!msg.type && 'role' in msg)) && 'role' in msg && msg.role === 'assistant') {
        // Look forward for the nearest stats message
        for (let j = i + 1; j < allMessages.length; j++) {
          const candidate = allMessages[j];
          if (candidate.type === 'stats' && 'stats' in candidate && candidate.stats.model) {
            modelByIndex.set(i, candidate.stats.model);
            break;
          }
          // Stop if we hit a user message (new turn)
          if ('role' in candidate && candidate.role === 'user') break;
        }
      }
    }

    const grouped = groupConsecutiveToolCalls(allMessages);
    return grouped.map((entry, groupIndex) => {
      if (entry.kind === 'tool_group') {
        return (
          <ToolCallGroup
            key={`group-${groupIndex}`}
            items={entry.items}
            subAgentToolCalls={subAgentToolCalls}
            subAgentReturned={subAgentReturned}
          />
        );
      }
      // Regular message with data-message-index for scroll-to-message
      return (
        <div key={`msg-${entry.index}`} data-message-index={entry.index}>
          <MessageContentRenderer
            message={entry.message}
            conversationId={conversationId}
            model={modelByIndex.get(entry.index)}
            onOpenAttachment={handleOpenAttachment}
          />
        </div>
      );
    });
  };

  // Render streaming messages with consecutive tool calls grouped
  const renderStreamingMessages = () => {
    if (!isStreaming || streamingMessages.length === 0) {
      return null;
    }

    const grouped = groupConsecutiveToolCalls(streamingMessages);
    return grouped.map((entry, groupIndex) => {
      if (entry.kind === 'tool_group') {
        return (
          <ToolCallGroup
            key={`streaming-group-${groupIndex}`}
            items={entry.items}
            subAgentToolCalls={subAgentToolCalls}
            subAgentReturned={subAgentReturned}
          />
        );
      }

      // Regular streaming message
      const msg = entry.message;

      // Handle action_request messages
      if (msg.type === 'action_request') {
        return (
          <MessageContentRenderer
            key={`streaming-action-request-${entry.index}`}
            message={msg}
            conversationId={conversationId}
          />
        );
      }

      // Render text messages from streaming
      if (msg.type === 'text' || (!msg.type && 'content' in msg && msg.content)) {
        return (
          <MessageContentRenderer
            key={`streaming-text-${entry.index}`}
            message={msg}
            conversationId={conversationId}
            model={selectedModel}
          />
        );
      }

      // Render stats messages
      if (msg.type === 'stats') {
        return (
          <MessageContentRenderer
            key={`streaming-stats-${entry.index}`}
            message={msg}
            conversationId={conversationId}
          />
        );
      }

      return null;
    });
  };

  // Loading state
  if (isLoading && !isLoaded) {
    return (
      <div className="chat-panel">
        <div className="chat-loading">
          Loading conversation...
        </div>
      </div>
    );
  }

  // Error state (only show if no messages and there's an error)
  if (error && !messages.length) {
    return (
      <div className="chat-panel">
        <div className="chat-error">
          {error}
        </div>
      </div>
    );
  }

  return (
    <div className="chat-panel">
      <MarkdownWorkspaceContext.Provider value={markdownWorkspaceCtx}>
      {/* Title unit + rename / archive menu (Claude-style). */}
      <ConversationHeader
        conversationId={conversationId}
        title={customName || conversationTitle}
        customName={customName}
        archived={archived}
        onRenamed={setCustomName}
        onArchivedChange={setArchived}
      />
      {/* Messages container */}
      <div
        className="messages-container"
        ref={containerRef}
        onScroll={handleScroll}
      >
        <div className="messages-list">
          {messages.length === 0 ? (
            <div className="empty-conversation">
              <p>Start a conversation by sending a message below</p>
            </div>
          ) : (
            renderGroupedMessages(messages)
          )}

          {/* Render streaming structured messages (tool uses) */}
          {renderStreamingMessages()}

          {/* Pre-stream typing indicator */}
          {isStreaming && !partialResponse && streamingMessages.length === 0 && (
            <div className="message assistant streaming">
              <div className="message-header">
                <span className="typing-indicator">
                  <span className="dot"></span>
                  <span className="dot"></span>
                  <span className="dot"></span>
                </span>
              </div>
              <div className="message-content typing-placeholder">
                Generating response...
              </div>
            </div>
          )}

          {/* Streaming message with markdown rendering */}
          {isStreaming && partialResponse && (
            <div className="message assistant streaming">
              <div className="message-header">
                <span className="typing-indicator">
                  <span className="dot"></span>
                  <span className="dot"></span>
                  <span className="dot"></span>
                </span>
              </div>
              <div className="message-content">
                <ReactMarkdown
                  remarkPlugins={[remarkGfm, remarkBreaks, remarkMath, remarkMathCurrencyGuard]}
                  rehypePlugins={[rehypeHighlight, rehypeKatex]}
                  components={markdownComponents}
                >
                  {partialResponse}
                </ReactMarkdown>
                <span className="streaming-cursor"></span>
              </div>
            </div>
          )}

          {/* Scroll anchor */}
          <div ref={messagesEndRef} />
        </div>
      </div>

      {/* Stream errors are now shown inline as error bubbles in the chat */}

      {/* Expensive-resume warning: blocks the composer on a long-idle,
          long-context conversation until the user picks an alternative or
          acknowledges the cost. The server only flags standalone non-Slack
          web conversations (project conversations never warn -- see
          chat/expensive_resume.py), and Slack-driven conversations are
          read-only anyway, so the card is suppressed there. */}
      {expensiveResumeBlocked && expensiveResume && !isReadOnly && (
        <ExpensiveResumeWarning
          info={expensiveResume}
          onCompact={handleExpensiveCompact}
          onDuplicateWorkspace={handleExpensiveDuplicateWorkspace}
          onCreateProject={handleOpenConvertModal}
          onContinueAnyway={acknowledgeExpensiveResume}
        />
      )}

      {/* Shared composer footer (textarea + Send/Stop + model selector +
          Skill button + Flags popover + attachments + ContextIndicator).
          Reused by the root home screen via HomeComposer. */}
      <Composer
        conversationId={conversationId}
        onSend={handleComposerSend}
        onStop={handleStopMessage}
        onAfterSend={handleAfterSend}
        isFirstMessage={messages.length === 0}
        isStreaming={isStreaming}
        isReadOnly={isReadOnly}
        hasPendingWait={hasPendingWait}
        pendingWaitKind={pendingWaitKind}
        pendingWaitCount={pendingWaitCount}
        expensiveResumeBlocked={expensiveResumeBlocked}
        contextTokens={contextTokens}
        maxContextTokens={maxContextTokens}
        conversationModel={conversationModel}
        conversationFlags={conversationFlags}
        slackThreadUrl={slackThreadUrl}
        isPublicProject={isPublicProject}
        readOnlyNotice={readOnlyNotice}
      />

      <ConvertToProjectModal
        isOpen={isConvertModalOpen}
        conversationId={conversationId}
        conversationTitle={conversationTitle}
        onClose={() => setIsConvertModalOpen(false)}
        onConverted={handleConvertedToProject}
      />

      {attachmentViewer && (
        <FileViewerModal
          isOpen={!!attachmentViewer}
          conversationId={conversationId}
          filePath={attachmentViewer.workspace_path}
          fileName={attachmentViewer.filename}
          isImage
          onClose={handleCloseAttachmentViewer}
          onDownload={handleDownloadAttachment}
        />
      )}
      </MarkdownWorkspaceContext.Provider>
    </div>
  );
});
