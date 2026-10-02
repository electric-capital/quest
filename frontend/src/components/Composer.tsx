/**
 * Composer -- the shared message-composer footer used by both the live chat
 * view (ChatPanel) and the root empty-state home screen (HomeComposer).
 *
 * This is the SINGLE composer codebase for the app. It owns all
 * composer-local state and handlers (text input + auto-resize, Enter-to-send,
 * clipboard-image paste/attachment queue, the model selector, the Skill
 * button + SkillSelectorModal, the Flags popover, and the ContextIndicator),
 * reading per-conversation model/skills/flags from
 * ``useConversationModels()`` / ``useConversationSkills()`` keyed by the
 * passed ``conversationId``.
 *
 * The host (ChatPanel for live chat, HomeComposer for the root screen) decides
 * the actual send behavior by passing an ``onSend`` callback and a set of
 * gating flags. ChatPanel passes its live ``sendMessage`` (with a real
 * conversation + WS subscription); HomeComposer passes a deferred-create flow
 * that creates the conversation and navigates. The component does NOT call
 * ``useConversation`` itself, so it can render before a conversation exists
 * (the home screen uses an in-memory draft key as the conversationId).
 *
 * Layout: ONE floating rounded bubble on every viewport (the ".composer-*"
 * styles in ChatPanel.css) -- the auto-growing textarea on top and an
 * icon-only controls row underneath (attach / skills / flags / model /
 * context / send). No text labels; the icon buttons carry tooltips.
 *
 * The only phone-vs-desktop differences (useIsMobile, same 768px breakpoint as
 * MobileShell) are behavioral: on desktop the controls row is always visible,
 * Enter sends and the textarea auto-focuses; on a phone the bubble collapses
 * to a single input pill until focused (or holding a draft) so it stays out
 * of the way of the conversation, Enter inserts a newline, and nothing
 * auto-focuses (that would pop the software keyboard). CSS keeps the
 * phone-only overlay/safe-area/popover-sheet rules behind the same media
 * query.
 */

import React, { useEffect, useRef, useState, useCallback } from 'react';
import { ArrowUp, Flag, Globe, LoaderCircle, Mic, Paperclip, Sparkles, Square } from 'lucide-react';
import { useAppConfig } from '../contexts/AppConfigContext';
import { useAuth } from '../contexts/AuthContext';
import { useConversationModels } from '../contexts/ConversationModelsContext';
import { useConversationSkills } from '../contexts/ConversationSkillsContext';
import { useIsMobile } from '../hooks/useIsMobile';
import { isVoiceInputSupported, useVoiceRecorder } from '../hooks/useVoiceRecorder';
import type { VoiceRecording } from '../hooks/useVoiceRecorder';
import type { ComposerAttachmentRef } from '../api/types';
import { uploadComposerAttachments } from '../api/fileApi';
import { transcribeAudio } from '../api/client';
import { ApiClientError } from '../api/request';
import { extractFilesFromDataTransfer } from '../utils/directoryTraversal';
import {
  getKnownModels, getSelectableModels, getProviderForModel, getModelDisplayName, getModelInfo,
  isModelAllowedFor,
} from '../constants/models';
import type { ModelVisibility } from '../constants/models';
import { getVisibleFlags, getFlagLabel } from '../constants/flags';
import { getFileIconInfo } from '../utils/fileIcons';
import { ContextIndicator } from './ContextIndicator';
import { ModelSelector } from './ModelSelector';
import { SkillSelectorModal } from './SkillSelectorModal';
import { SystemPromptModal } from './SystemPromptModal';

/**
 * A clipboard-pasted image queued in the composer. Stays in component-local
 * state until the user sends or removes it; on send we upload the bytes to
 * the composer-attachments endpoint and then forward the returned refs in
 * the ``send_message`` WS envelope.
 */
interface PastedImage {
  id: string;
  file: File;
  mimeType: 'image/png' | 'image/jpeg';
  previewUrl: string;
  sizeBytes: number;
}

// Hard cap on queued attachments per send. Mirrors the server-side validation
// in chat/realtime/socket.py and chat/file_routes.py.
const MAX_COMPOSER_ATTACHMENTS = 10;

// Per-file size cap for generic workspace file attachments. Mirrors the
// backend MAX_FILE_SIZE in chat/file_routes.py (200 MB) so we reject oversize
// files client-side before the (potentially huge) upload.
const MAX_ATTACH_FILE_SIZE = 200 * 1024 * 1024;

// Sanity cap on the number of generic files queued per send. The backend has
// no hard count limit; this just keeps the UI manageable.
const MAX_PENDING_FILES = 20;

// A generic workspace file queued in the composer awaiting upload. Kept
// separate from the image paste queue (PastedImage) -- different upload target
// (/files/upload) and semantics (no inline multimodal bytes).
interface QueuedFile {
  id: string;
  file: File;
}

/** Human-readable byte size for the file chips (e.g. "12.3 KB"). */
function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  const units = ['KB', 'MB', 'GB'];
  let size = bytes / 1024;
  let unitIndex = 0;
  while (size >= 1024 && unitIndex < units.length - 1) {
    size /= 1024;
    unitIndex += 1;
  }
  return `${size.toFixed(1)} ${units[unitIndex]}`;
}

// Composer paste only accepts PNG and JPEG. Other clipboard image types
// (gif/webp/svg) are surfaced as an inline rejection notice.
const ALLOWED_PASTE_MIME_TYPES = new Set<'image/png' | 'image/jpeg'>([
  'image/png',
  'image/jpeg',
]);

export interface ComposerProps {
  /**
   * The conversation this composer is bound to. For the live chat this is the
   * real conversation id; for the home screen it is a stable in-memory draft
   * key used only for the context-keyed model/skills lookups.
   */
  conversationId: string;
  /**
   * The send callback. ChatPanel passes its live ``sendMessage``; HomeComposer
   * passes the deferred-create flow. Same signature as
   * ``useConversation.sendMessage``. The composer clears its input/attachments
   * before invoking this and runs the two-phase upload for pasted images.
   */
  onSend: (
    text: string,
    model: string,
    skillIds?: string[],
    attachments?: ComposerAttachmentRef[],
    flags?: string[],
    files?: File[],
    /**
     * Raw pasted images, passed INSTEAD of ``attachments`` when
     * ``deferImageUpload`` is set: the host owns the upload because no
     * conversation exists yet (home screen). Undefined otherwise.
     */
    imageFiles?: File[],
  ) => void;
  /**
   * Stop/interrupt callback. When provided AND ``isStreaming`` is true, the
   * Send button is replaced by a Stop button. The home screen never streams,
   * so it omits this.
   */
  onStop?: () => void;
  /**
   * Optional hook invoked right after a send is dispatched (used by ChatPanel
   * to scroll the message list to the bottom). HomeComposer omits it.
   */
  onAfterSend?: () => void;
  /**
   * Whether this is the first message of the conversation (i.e.
   * ``messages.length === 0``). Drives the interactive Flags popover vs. the
   * read-only flags label.
   */
  isFirstMessage: boolean;
  /**
   * When true, sending does NOT write the first-send provider lock for
   * ``conversationId``. Set by HomeComposer: its composer is bound to an
   * in-memory draft key (HOME_DRAFT_KEY) with no real conversation, and the
   * host (ChatPanel's pendingFirstMessage effect) performs the provider
   * locking on the real id after navigation. Locking the draft key would
   * permanently poison the persisted lock maps (the entry is never removed)
   * and filter the home model dropdown to one provider forever. Defaults to
   * false: ChatPanel's per-conversation composer must keep locking on send.
   */
  skipSendLocks?: boolean;
  /**
   * When true, pasted images are NOT uploaded by the composer on send (the
   * composer-attachments endpoint needs a real conversation id, and
   * HOME_DRAFT_KEY is not one). Instead the raw files are handed to
   * ``onSend`` as ``imageFiles`` so the host can upload them once the
   * conversation exists. Set by HomeComposer; defaults to false.
   */
  deferImageUpload?: boolean;
  // ---- Gating flags (from the host's useConversation, or constants on home)
  isStreaming?: boolean;
  isReadOnly?: boolean;
  hasPendingWait?: boolean;
  pendingWaitKind?: string | null;
  /** How many wait handles are pending; >1 when one turn opened several cards. */
  pendingWaitCount?: number;
  /**
   * True while the conversation is blocked behind the expensive-resume
   * warning card (rendered by ChatPanel above this composer). Disables
   * input until the user picks an alternative or clicks "Continue anyway".
   */
  expensiveResumeBlocked?: boolean;
  // ---- ContextIndicator props (null/zero on the home screen)
  contextTokens?: number | null;
  maxContextTokens?: number | null;
  /**
   * Raw per-conversation model value (from the conversation record). Only used
   * to render the disabled model select in the Slack read-only case.
   */
  conversationModel?: string | null;
  /**
   * Persisted per-conversation flags (hydrated from the GET response). Drives
   * the post-first-message read-only flags label. Empty on the home screen.
   */
  conversationFlags?: string[];
  /** Deep link to the driving Slack thread (read-only case only). */
  slackThreadUrl?: string | null;
  /**
   * The conversation lives in a PUBLIC project (internet-enabled sandbox,
   * no internal data -- see docs/architecture/public-projects.md). Set by
   * ChatPanel from the fetched project's ``public`` flag. Drives two things:
   * a persistent warning banner above the input reminding the user that
   * anything they type may be sent to third-party websites, and hiding the
   * Skill button (skills are internal data and the backend ignores
   * skill_ids there anyway).
   */
  isPublicProject?: boolean;
  /**
   * Non-Slack read-only banner copy (cross-user subagent conversations).
   * When set, the read-only banner shows this text instead of the Slack
   * copy/link.
   */
  readOnlyNotice?: string | null;
  /**
   * Draft-safe model setter override. The home screen passes a setter that
   * updates only the in-memory model map + defaultModel (no PATCH to a
   * nonexistent conversation). When omitted, the context's
   * ``setModelForConversation`` is used (the live-chat behavior, which also
   * PATCHes the conversation's model).
   */
  onModelChange?: (model: string) => void;
}

/**
 * The shared composer. See module docstring.
 */
/** mm:ss for the recording timer beside the mic button. */
function formatRecordingTime(totalSeconds: number): string {
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return `${minutes}:${seconds.toString().padStart(2, '0')}`;
}

export function Composer({
  conversationId,
  onSend,
  onStop,
  onAfterSend,
  isFirstMessage,
  skipSendLocks = false,
  deferImageUpload = false,
  isStreaming = false,
  isReadOnly = false,
  hasPendingWait = false,
  pendingWaitKind = null,
  pendingWaitCount = 0,
  expensiveResumeBlocked = false,
  contextTokens = null,
  maxContextTokens = null,
  conversationModel = null,
  conversationFlags = [],
  slackThreadUrl = null,
  isPublicProject = false,
  readOnlyNotice = null,
  onModelChange,
}: ComposerProps) {
  const [inputValue, setInputValue] = useState('');
  const [pendingAttachments, setPendingAttachments] = useState<PastedImage[]>([]);
  // Generic workspace files queued for upload-before-send (distinct from the
  // image paste queue above). The host owns the actual upload; the composer
  // just collects the raw File[] and hands them off via onSend.
  const [pendingFiles, setPendingFiles] = useState<QueuedFile[]>([]);
  const [pasteNotice, setPasteNotice] = useState<string | null>(null);
  const [isUploadingAttachments, setIsUploadingAttachments] = useState(false);
  // Voice input: true from the moment a recording finishes until the
  // transcript request resolves (the mic button shows a spinner meanwhile).
  const [isTranscribing, setIsTranscribing] = useState(false);
  // Voice input: armed when Send (button or Enter) is pressed mid-recording.
  // The recording is stopped, transcribed, appended to the draft, and the
  // draft is sent as soon as the transcript has landed (see the effect
  // below handleSendMessage). Disarmed without sending when transcription
  // fails or hears no speech, so the user can decide what to do. The ref
  // is the source of truth (it is cleared from effects that cancel the
  // recording); the state only drives the placeholder text.
  const sendAfterTranscriptionRef = useRef(false);
  const [sendAfterTranscription, setSendAfterTranscription] = useState(false);
  const armSendAfterTranscription = useCallback((armed: boolean) => {
    sendAfterTranscriptionRef.current = armed;
    setSendAfterTranscription(armed);
  }, []);
  const [isSkillModalOpen, setIsSkillModalOpen] = useState(false);
  const [isSystemPromptModalOpen, setIsSystemPromptModalOpen] = useState(false);
  // Per-conversation flags selected via the composer Flags popover. Only
  // meaningful on the first message (flags lock at conversation start); reset
  // when the conversation switches.
  const [isFlagsOpen, setIsFlagsOpen] = useState(false);
  const [selectedFlags, setSelectedFlags] = useState<string[]>([]);
  // True while a file drag is hovering the composer (drives the drop overlay).
  const [isDragOver, setIsDragOver] = useState(false);
  // Phone-width layout switch (same 768px breakpoint as MobileShell).
  const isMobile = useIsMobile();
  // Focus-within tracking for the mobile floating bar: focused (or holding a
  // draft) = expanded two-line bubble, otherwise the collapsed pill.
  const [isFocusWithin, setIsFocusWithin] = useState(false);
  // True while the model menu is open. Its phone form is a full-screen sheet
  // that takes focus (dropping the keyboard), which must not collapse the
  // bubble: the controls row hosts the menu.
  const [isModelMenuOpen, setIsModelMenuOpen] = useState(false);

  const inputRef = useRef<HTMLTextAreaElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const flagsMenuRef = useRef<HTMLDivElement>(null);
  const prevStreamingRef = useRef(false);
  // dragenter/dragleave fire for every child element crossed; a depth counter
  // keeps the overlay stable instead of flickering while hovering children.
  const dragDepthRef = useRef(0);

  const {
    getModelForConversation, setModelForConversation,
    getLockedProvider, lockConversationProvider, isProviderLocked,
  } = useConversationModels();
  const {
    getQueuedSkillsForConversation, setQueuedSkillsForConversation,
    getLoadedSkillsForConversation,
    markSkillsAsLoaded,
  } = useConversationSkills();
  const { availableModelIds } = useAppConfig();
  const { enabledFeatures } = useAuth();

  // Models whose backend credentials are configured server-side. null means
  // the config fetch hasn't resolved yet -- treat as "all available".
  // Deprecated models and models the admin has not allowed for this
  // conversation's visibility (Settings > Model Selection: private vs
  // public-project conversations) are excluded here (not offered for
  // selection); a conversation already on one keeps it via ModelSelector's
  // current-selection fallback.
  const modelVisibility: ModelVisibility = isPublicProject ? 'public' : 'private';
  const credentialedModels = availableModelIds === null
    ? getSelectableModels(modelVisibility)
    : getSelectableModels(modelVisibility).filter((m) => availableModelIds.includes(m.id));
  const noModelsAvailable = availableModelIds !== null && availableModelIds.length === 0;

  const selectedModel = getModelForConversation(conversationId);
  // A known model the admin has unticked for this visibility cannot be sent
  // with (the server refuses the turn too); the menu keeps it visible with a
  // "(not allowed here)" suffix so the user can switch away.
  const selectedModelInfo = getModelInfo(selectedModel);
  const selectedModelDisallowed = selectedModelInfo !== undefined
    && !isModelAllowedFor(selectedModelInfo, modelVisibility);
  const queuedSkills = getQueuedSkillsForConversation(conversationId);
  const loadedSkills = getLoadedSkillsForConversation(conversationId);

  // Input is disabled during streaming, when the conversation is driven
  // from Slack, when the agent is suspended on a pending wait handle
  // (action_request awaiting approval, slack_reply awaiting Slack DM), or
  // while the expensive-resume warning card awaits acknowledgement.
  const inputDisabled = isStreaming || isReadOnly || hasPendingWait || expensiveResumeBlocked;

  // Placeholder copy varies with why the composer is locked. The Slack
  // case has its own banner above the textarea; for the wait-handle case
  // we surface a short hint inline.
  let composerPlaceholder: string;
  if (isReadOnly) {
    composerPlaceholder = readOnlyNotice
      ? "This conversation is read-only."
      : "Replying is disabled — use Slack.";
  } else if (hasPendingWait && pendingWaitKind === 'action_request') {
    composerPlaceholder = pendingWaitCount > 1
      ? `Approve, revise, or stop the ${pendingWaitCount} pending requests to continue.`
      : "Approve, revise, or stop the pending request to continue.";
  } else if (hasPendingWait && pendingWaitKind === 'user_subagent') {
    composerPlaceholder = "Waiting for the subagent to return its response...";
  } else if (hasPendingWait) {
    composerPlaceholder = "Waiting for the pending tool call to resolve.";
  } else if (expensiveResumeBlocked) {
    composerPlaceholder = "Choose an option in the cost warning above to continue.";
  } else {
    // Mobile keeps the collapsed pill quiet; the desktop hint mentions the
    // Enter-to-send binding, which mobile doesn't have.
    composerPlaceholder = isMobile
      ? "Message"
      : "Type a message... (Enter to send, Shift+Enter for newline)";
  }

  // ----------------------------------------------------------------
  // Voice input (microphone -> server-side transcription)
  // ----------------------------------------------------------------

  // Re-run the textarea auto-resize after a programmatic value change
  // (handleInputChange only runs for user typing).
  const resizeInput = useCallback(() => {
    const el = inputRef.current;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = `${Math.min(el.scrollHeight, isMobile ? 160 : 200)}px`;
  }, [isMobile]);

  const handleRecordingComplete = useCallback(async (recording: VoiceRecording) => {
    setIsTranscribing(true);
    try {
      const result = await transcribeAudio(recording.blob, recording.filename);
      const text = result.text.trim();
      if (!text) {
        armSendAfterTranscription(false);
        setPasteNotice('No speech was detected in the recording.');
        return;
      }
      // Append to whatever is already drafted, separated by a space, so a
      // second clip continues the first instead of replacing it.
      setInputValue((prev) => {
        const base = prev.replace(/\s+$/, '');
        return base ? `${base} ${text}` : text;
      });
      requestAnimationFrame(() => {
        resizeInput();
        // Desktop only: focusing on a phone pops the keyboard.
        if (!isMobile) inputRef.current?.focus();
      });
    } catch (error) {
      armSendAfterTranscription(false);
      console.error('Transcription failed:', error);
      const code = error instanceof ApiClientError ? error.errorCode : undefined;
      setPasteNotice(
        code === 'voice_input_disabled'
          ? 'Voice input has been turned off by an administrator.'
          : code === 'transcription_unavailable'
            ? 'Voice input is not available right now (no transcription model is configured).'
            : `Could not transcribe the recording${error instanceof Error && error.message ? `: ${error.message}` : '.'}`,
      );
    } finally {
      setIsTranscribing(false);
    }
  }, [isMobile, resizeInput, armSendAfterTranscription]);

  const handleRecordingError = useCallback((message: string) => {
    armSendAfterTranscription(false);
    setPasteNotice(message);
  }, [armSendAfterTranscription]);

  const {
    status: recorderStatus,
    elapsedSeconds: recordingSeconds,
    inputLevel: recordingLevel,
    start: startRecording,
    stop: stopRecording,
    cancel: cancelRecording,
  } = useVoiceRecorder({
    onRecordingComplete: handleRecordingComplete,
    onError: handleRecordingError,
  });
  const isRecording = recorderStatus !== 'idle';

  // The button appears only when an admin has enabled the feature for this
  // user AND the browser can capture audio here (secure context + MediaRecorder).
  const voiceInputEnabled = enabledFeatures.includes('voice_input') && isVoiceInputSupported();

  const handleMicClick = useCallback(() => {
    if (recorderStatus === 'recording') {
      stopRecording();
    } else if (recorderStatus === 'idle') {
      void startRecording();
    }
  }, [recorderStatus, startRecording, stopRecording]);

  // Drop an in-progress recording when the composer locks or the
  // conversation switches: the clip belonged to the previous context.
  useEffect(() => {
    if (inputDisabled) {
      sendAfterTranscriptionRef.current = false;
      cancelRecording();
    }
  }, [inputDisabled, cancelRecording]);
  useEffect(() => {
    sendAfterTranscriptionRef.current = false;
    cancelRecording();
  }, [conversationId, cancelRecording]);

  if (recorderStatus === 'starting') {
    composerPlaceholder = 'Starting the microphone… one moment before you speak.';
  } else if (recorderStatus === 'recording') {
    composerPlaceholder = 'Listening… click the microphone to stop, or Send to transcribe and send.';
  } else if (isTranscribing) {
    composerPlaceholder = sendAfterTranscription ? 'Transcribing, then sending…' : 'Transcribing…';
  }

  // Auto-focus the textarea when streaming ends (so the user can type
  // immediately), and on mount when the composer is fresh (covers the home
  // "pre-focused on load" case and a freshly-created empty conversation).
  // Never on mobile: programmatic focus would pop the software keyboard (and
  // expand the floating bar) without a user tap.
  useEffect(() => {
    if (!isMobile && !isStreaming && prevStreamingRef.current) {
      inputRef.current?.focus();
    }
    prevStreamingRef.current = isStreaming;
  }, [isStreaming, isMobile]);

  // Focus on mount when there are no messages yet (home screen + fresh chat).
  useEffect(() => {
    if (isFirstMessage && !inputDisabled && !isMobile) {
      inputRef.current?.focus();
    }
    // Intentionally mount-only (and on conversation switch via the key below);
    // we don't want to steal focus on every render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [conversationId]);

  // Handle model change. Uses the draft-safe override on the home screen
  // (no PATCH to a nonexistent conversation), otherwise the context setter.
  const handleModelSelect = useCallback((modelId: string) => {
    if (onModelChange) {
      onModelChange(modelId);
    } else {
      setModelForConversation(conversationId, modelId);
    }
  }, [conversationId, setModelForConversation, onModelChange]);

  // Handle skill modal confirm
  const handleSkillConfirm = useCallback((selectedSkillIds: string[]) => {
    if (selectedSkillIds.length > 0) {
      setQueuedSkillsForConversation(conversationId, selectedSkillIds);
    }
  }, [conversationId, setQueuedSkillsForConversation]);

  // Handle send message
  const handleSendMessage = useCallback(() => {
    if (
      isStreaming
      || isUploadingAttachments
      || hasPendingWait
      || expensiveResumeBlocked
    ) return;

    // Send pressed mid-dictation: stop the recording now and send once the
    // transcript has been appended to the draft (effect below).
    if (isRecording) {
      armSendAfterTranscription(true);
      stopRecording();
      return;
    }
    if (isTranscribing) return;
    if (sendAfterTranscriptionRef.current) armSendAfterTranscription(false);

    const trimmedInput = inputValue.trim();
    const hasAttachments = pendingAttachments.length > 0;
    if (!trimmedInput && !hasAttachments) return;

    // Optimistically clear composer text immediately; attachments are kept
    // queued until the upload completes so we can re-surface them on error.
    setInputValue('');

    // Reset textarea height
    if (inputRef.current) {
      inputRef.current.style.height = 'auto';
    }

    // Lock the provider for this conversation on first message. Skipped in
    // draft mode (skipSendLocks, home screen): there is no real conversation
    // yet, and ChatPanel's pendingFirstMessage effect locks the real id after
    // navigation.
    if (!skipSendLocks) {
      if (!isProviderLocked(conversationId)) {
        lockConversationProvider(conversationId, getProviderForModel(selectedModel));
      }
    }

    const skillIdsToSend = queuedSkills.length > 0 ? queuedSkills : undefined;

    // Flags only matter on the very first message (they lock at conversation
    // start). On later sends pass undefined so the field is omitted.
    const flagsToSend = isFirstMessage && selectedFlags.length > 0 ? selectedFlags : undefined;

    // Snapshot the queued generic files and hand them off to the host, which
    // owns the upload (the conversation may not exist yet on the home screen).
    // Clear the queue optimistically on dispatch; the host surfaces any upload
    // failure via its own error path.
    const filesToSend = pendingFiles.length > 0
      ? pendingFiles.map((f) => f.file)
      : undefined;

    const dispatchSend = (attachments?: ComposerAttachmentRef[], imageFiles?: File[]) => {
      onAfterSend?.();
      onSend(trimmedInput, selectedModel, skillIdsToSend, attachments, flagsToSend, filesToSend, imageFiles);
      if (filesToSend) {
        setPendingFiles([]);
      }
      if (queuedSkills.length > 0) {
        markSkillsAsLoaded(conversationId, queuedSkills);
      }
    };

    if (!hasAttachments) {
      dispatchSend(undefined);
      return;
    }

    // Draft mode (home screen): there is no conversation to upload against
    // yet, so hand the raw files to the host and clear the queue the same way
    // generic files are cleared -- the host surfaces any upload failure.
    if (deferImageUpload) {
      const imageFiles = pendingAttachments.map((a) => a.file);
      for (const att of pendingAttachments) {
        try { URL.revokeObjectURL(att.previewUrl); } catch { /* ignore */ }
      }
      setPendingAttachments([]);
      dispatchSend(undefined, imageFiles);
      return;
    }

    // Two-phase send: upload pasted images first, then forward the refs.
    setIsUploadingAttachments(true);
    const filesToUpload = pendingAttachments.map((a) => a.file);
    uploadComposerAttachments(conversationId, filesToUpload)
      .then((resp) => {
        if (resp.errors && resp.errors.length > 0) {
          // Surface a generic notice; the server already rejected the bad
          // ones, so we don't try to map back to individual queued items.
          setPasteNotice(
            `Some attachments could not be uploaded: ${resp.errors.map((e) => e.message).join('; ')}`,
          );
        }
        if (!resp.attachments || resp.attachments.length === 0) {
          // Re-surface text input so the user can retry.
          setInputValue(trimmedInput);
          return;
        }
        // Successful upload: revoke object URLs and clear the queued list.
        for (const att of pendingAttachments) {
          try { URL.revokeObjectURL(att.previewUrl); } catch { /* ignore */ }
        }
        setPendingAttachments([]);
        dispatchSend(resp.attachments);
      })
      .catch((err) => {
        const message = err instanceof Error ? err.message : 'Failed to upload attachments';
        setPasteNotice(message);
        // Re-surface text input so the user can retry without retyping.
        setInputValue(trimmedInput);
      })
      .finally(() => {
        setIsUploadingAttachments(false);
      });
  }, [inputValue, pendingAttachments, pendingFiles, isStreaming, isUploadingAttachments, hasPendingWait, expensiveResumeBlocked, isRecording, isTranscribing, stopRecording, armSendAfterTranscription, onSend, onAfterSend, selectedModel, skipSendLocks, deferImageUpload, conversationId, isProviderLocked, lockConversationProvider, queuedSkills, markSkillsAsLoaded, isFirstMessage, selectedFlags]);

  // Complete a Send that was pressed mid-dictation. Runs once the recorder
  // is idle and the transcript request has resolved; by then the transcript
  // is in inputValue (React batches the recorder's idle transition with the
  // transcribing flag, so this never fires between the two).
  useEffect(() => {
    if (!sendAfterTranscriptionRef.current || isRecording || isTranscribing) return;
    handleSendMessage();
  }, [isRecording, isTranscribing, handleSendMessage]);

  // Handle keyboard input. On mobile, Enter inserts a newline (software
  // keyboards have no Shift+Enter; sending is the arrow button's job).
  const handleKeyDown = useCallback((e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (isMobile) return;
    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      handleSendMessage();
    }
  }, [handleSendMessage, isMobile]);

  // Auto-resize textarea. The mobile cap is lower: with the software keyboard
  // up, the visual viewport is short and a 200px input would crowd out the
  // conversation.
  const handleInputChange = useCallback((e: React.ChangeEvent<HTMLTextAreaElement>) => {
    setInputValue(e.target.value);

    // Auto-resize
    const target = e.target;
    target.style.height = 'auto';
    const newHeight = Math.min(target.scrollHeight, isMobile ? 160 : 200);
    target.style.height = `${newHeight}px`;
  }, [isMobile]);

  // ----------------------------------------------------------------
  // Composer paste / attachment handling
  // ----------------------------------------------------------------

  // Clear queued attachments when navigating to a different conversation:
  // PastedImage is component-local state and the previewUrl object-URLs are
  // tied to the user's intent to send in *this* conversation. Revoke them
  // to avoid blob leaks.
  useEffect(() => {
    setPendingAttachments((prev) => {
      for (const att of prev) {
        try { URL.revokeObjectURL(att.previewUrl); } catch { /* ignore */ }
      }
      return [];
    });
    // Generic file queue is also per-conversation intent; clear it so a queue
    // doesn't bleed across the home-mount cycle or into a navigated chat.
    setPendingFiles([]);
    setPasteNotice(null);
    // Flags are a start-of-conversation selection; clear the local popover
    // selection when switching conversations so they never bleed across.
    setSelectedFlags([]);
    setIsFlagsOpen(false);
  }, [conversationId]);

  // Close the Flags popover on an outside click (AdminOpsMenu pattern).
  useEffect(() => {
    if (!isFlagsOpen) return;
    const handleClickOutside = (event: MouseEvent) => {
      if (flagsMenuRef.current && !flagsMenuRef.current.contains(event.target as Node)) {
        setIsFlagsOpen(false);
      }
    };
    document.addEventListener('mousedown', handleClickOutside);
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, [isFlagsOpen]);

  // Auto-dismiss the inline paste-rejection notice after a few seconds so
  // it doesn't linger above the composer.
  useEffect(() => {
    if (!pasteNotice) return;
    const t = window.setTimeout(() => setPasteNotice(null), 4000);
    return () => window.clearTimeout(t);
  }, [pasteNotice]);

  const removeAttachment = useCallback((id: string) => {
    setPendingAttachments((prev) => {
      const target = prev.find((a) => a.id === id);
      if (target) {
        try { URL.revokeObjectURL(target.previewUrl); } catch { /* ignore */ }
      }
      return prev.filter((a) => a.id !== id);
    });
  }, []);

  // ----------------------------------------------------------------
  // Generic file attachment handling (uploaded by the host before send)
  // ----------------------------------------------------------------

  // Open the hidden file picker (FileBrowser pattern).
  const handleAttachClick = useCallback(() => {
    fileInputRef.current?.click();
  }, []);

  // Validate and append files to the pending queue: drop oversize ones with
  // an inline notice and cap the total queue length. Shared by the hidden
  // file picker and composer drag-and-drop.
  const queueFiles = useCallback((incoming: File[]) => {
    if (incoming.length === 0) return;
    const accepted: QueuedFile[] = [];
    const oversized: string[] = [];
    for (let i = 0; i < incoming.length; i++) {
      const file = incoming[i];
      if (file.size > MAX_ATTACH_FILE_SIZE) {
        oversized.push(file.name);
        continue;
      }
      accepted.push({
        id: `file-${Date.now()}-${i}-${Math.random().toString(36).slice(2, 8)}`,
        file,
      });
    }
    if (oversized.length > 0) {
      setPasteNotice(
        oversized.length === 1
          ? `"${oversized[0]}" exceeds the 200 MB limit and was not attached.`
          : `${oversized.length} files exceed the 200 MB limit and were not attached.`,
      );
    }
    if (accepted.length > 0) {
      setPendingFiles((prev) => {
        const merged = [...prev, ...accepted];
        if (merged.length > MAX_PENDING_FILES) {
          setPasteNotice(
            `At most ${MAX_PENDING_FILES} files can be attached; extra files were dropped.`,
          );
          return merged.slice(0, MAX_PENDING_FILES);
        }
        return merged;
      });
    }
  }, []);

  // Read the chosen files and queue them. Resets input.value so picking the
  // same file again re-fires onChange.
  const handleFileInputChange = useCallback(
    (e: React.ChangeEvent<HTMLInputElement>) => {
      const selected = e.target.files;
      if (selected && selected.length > 0) {
        queueFiles(Array.from(selected));
      }
      // Reset so re-selecting the same file fires onChange again.
      if (fileInputRef.current) {
        fileInputRef.current.value = '';
      }
    },
    [queueFiles],
  );

  // ----------------------------------------------------------------
  // Drag-and-drop onto the composer (same queue as the Attach button)
  // ----------------------------------------------------------------

  // Only react to drags that actually carry files -- dragging selected text
  // across the composer must not light up the drop overlay.
  const dragHasFiles = (e: React.DragEvent) =>
    Array.from(e.dataTransfer.types).includes('Files');

  const handleDragEnter = useCallback((e: React.DragEvent<HTMLDivElement>) => {
    if (inputDisabled || !dragHasFiles(e)) return;
    e.preventDefault();
    e.stopPropagation();
    dragDepthRef.current += 1;
    setIsDragOver(true);
  }, [inputDisabled]);

  const handleDragOver = useCallback((e: React.DragEvent<HTMLDivElement>) => {
    if (inputDisabled || !dragHasFiles(e)) return;
    e.preventDefault();
    e.stopPropagation();
  }, [inputDisabled]);

  const handleDragLeave = useCallback((e: React.DragEvent<HTMLDivElement>) => {
    if (!dragHasFiles(e)) return;
    e.preventDefault();
    e.stopPropagation();
    dragDepthRef.current = Math.max(0, dragDepthRef.current - 1);
    if (dragDepthRef.current === 0) {
      setIsDragOver(false);
    }
  }, []);

  const handleDrop = useCallback(async (e: React.DragEvent<HTMLDivElement>) => {
    if (!dragHasFiles(e)) return;
    e.preventDefault();
    e.stopPropagation();
    dragDepthRef.current = 0;
    setIsDragOver(false);
    if (inputDisabled) return;
    // Traverse dropped folders too; the composer attach flow is flat (names
    // threaded as attached_filenames), so nested files are queued by name.
    const filesWithPaths = await extractFilesFromDataTransfer(e.dataTransfer);
    queueFiles(filesWithPaths.map((f) => f.file));
  }, [inputDisabled, queueFiles]);

  const removeQueuedFile = useCallback((id: string) => {
    setPendingFiles((prev) => prev.filter((f) => f.id !== id));
  }, []);

  const handlePaste = useCallback(
    (event: React.ClipboardEvent<HTMLTextAreaElement>) => {
      if (inputDisabled) return;
      const items = event.clipboardData?.items;
      if (!items || items.length === 0) return;

      const accepted: PastedImage[] = [];
      let rejectedCount = 0;
      let imageItemSeen = false;

      for (let i = 0; i < items.length; i++) {
        const item = items[i];
        if (item.kind !== 'file') continue;
        const mime = item.type;
        if (!mime.startsWith('image/')) continue;
        imageItemSeen = true;
        if (!ALLOWED_PASTE_MIME_TYPES.has(mime as 'image/png' | 'image/jpeg')) {
          rejectedCount += 1;
          continue;
        }
        const file = item.getAsFile();
        if (!file) continue;
        accepted.push({
          id: `paste-${Date.now()}-${i}-${Math.random().toString(36).slice(2, 8)}`,
          file,
          mimeType: mime as 'image/png' | 'image/jpeg',
          previewUrl: URL.createObjectURL(file),
          sizeBytes: file.size,
        });
      }

      // Nothing image-shaped on the clipboard -- let the textarea default
      // text-paste behavior run.
      if (!imageItemSeen) return;

      // Prevent the default ONLY when we captured at least one image; if
      // every image item was rejected, also prevent default to avoid the
      // browser pasting binary garbage into the textarea.
      event.preventDefault();

      if (accepted.length === 0 && rejectedCount > 0) {
        setPasteNotice(
          rejectedCount === 1
            ? 'Only PNG and JPEG images can be pasted; ignoring 1 attachment.'
            : `Only PNG and JPEG images can be pasted; ignoring ${rejectedCount} attachments.`,
        );
        return;
      }

      if (accepted.length === 0) return;

      setPendingAttachments((prev) => {
        const merged = [...prev, ...accepted];
        if (merged.length > MAX_COMPOSER_ATTACHMENTS) {
          const dropped = merged.slice(MAX_COMPOSER_ATTACHMENTS);
          for (const d of dropped) {
            try { URL.revokeObjectURL(d.previewUrl); } catch { /* ignore */ }
          }
          setPasteNotice(
            `At most ${MAX_COMPOSER_ATTACHMENTS} attachments can be queued; extra images were dropped.`,
          );
          return merged.slice(0, MAX_COMPOSER_ATTACHMENTS);
        }
        if (rejectedCount > 0) {
          setPasteNotice(
            'Some clipboard items were skipped because only PNG and JPEG images can be pasted.',
          );
        }
        return merged;
      });
    },
    // ``inputDisabled`` is the only outer state the paste handler reads;
    // the rest is local to the function or self-stable setState callbacks.
    [inputDisabled],
  );

  // ----------------------------------------------------------------
  // Bubble expansion (phone only)
  // ----------------------------------------------------------------

  // Desktop always shows the controls row. On a phone the bubble is a bare
  // input pill until focus is inside the composer OR any draft state exists
  // (text, attachments, queued skills/flags), so the controls -- and their
  // badges -- never hide while something is pending. A read-only phone
  // composer stays collapsed (nothing to control). The open model sheet also
  // holds the bubble expanded, since it moves focus out of the composer.
  const hasDraftState = inputValue.length > 0
    || pendingAttachments.length > 0
    || pendingFiles.length > 0
    || queuedSkills.length > 0
    || selectedFlags.length > 0
    || isRecording
    || isTranscribing;
  const expanded = isMobile
    ? (!isReadOnly && (isFocusWithin || hasDraftState || isModelMenuOpen))
    : true;

  const handleRootFocus = useCallback(() => setIsFocusWithin(true), []);
  const handleRootBlur = useCallback((e: React.FocusEvent<HTMLDivElement>) => {
    if (!e.currentTarget.contains(e.relatedTarget as Node)) {
      setIsFocusWithin(false);
    }
  }, []);

  // ----------------------------------------------------------------
  // Layout fragments
  // ----------------------------------------------------------------

  // Provider-lock-filtered model list offered by the selector.
  const lockedProvider = getLockedProvider(conversationId);
  const selectableModels = lockedProvider
    ? credentialedModels.filter((m) => m.provider === lockedProvider)
    : credentialedModels;

  const sendDisabled =
    (!inputValue.trim() && pendingAttachments.length === 0 && !isRecording)
    || inputDisabled
    || isUploadingAttachments
    || isTranscribing
    || noModelsAvailable
    || selectedModelDisallowed;

  // Persistent public-project reminder above the input. Public conversations
  // run in an internet-enabled sandbox where the model may submit content to
  // arbitrary third-party websites, so the user must not paste anything
  // private or sensitive. Rendered on EVERY composer bound to a public
  // project (desktop and mobile, every message -- not just the first).
  const publicProjectBanner = isPublicProject ? (
    <div className="public-project-warning" role="note">
      <Globe size={14} className="public-project-warning-icon" aria-hidden="true" />
      <span>
        <strong>Public project:</strong> this conversation has internet access, and anything
        you write here may be sent to third-party websites. Do not include private or
        sensitive information.
      </span>
    </div>
  ) : null;

  const readOnlyBanner = isReadOnly ? (
    <div className="slack-thread-link-note">
      {readOnlyNotice ? (
        <>{readOnlyNotice}</>
      ) : slackThreadUrl ? (
        <>
          Replying is disabled — use Slack:{' '}
          <a href={slackThreadUrl} target="_blank" rel="noopener noreferrer">
            Open Slack thread
          </a>
        </>
      ) : (
        <>Replying is disabled — use Slack.</>
      )}
    </div>
  ) : null;

  const pasteNoticeEl = pasteNotice ? (
    <div className="paste-notice" role="status">{pasteNotice}</div>
  ) : null;

  const pastedImagesRow = pendingAttachments.length > 0 ? (
    <div className="paste-attachments-row">
      {pendingAttachments.map((att) => (
        <div key={att.id} className="paste-attachment-thumb">
          <img src={att.previewUrl} alt={att.file.name || 'pasted image'} />
          <button
            type="button"
            className="paste-attachment-remove"
            onClick={() => removeAttachment(att.id)}
            title="Remove attachment"
            aria-label="Remove attachment"
            disabled={isUploadingAttachments}
          >
            x
          </button>
        </div>
      ))}
    </div>
  ) : null;

  const queuedFilesRow = pendingFiles.length > 0 ? (
    <div className="file-attachments-row">
      {pendingFiles.map(({ id, file }) => {
        const { Icon, className } = getFileIconInfo(file.name);
        return (
          <div key={id} className="file-attachment-chip">
            <Icon size={14} className={className} />
            <span className="file-attachment-name" title={file.name}>{file.name}</span>
            <span className="file-attachment-size">{formatFileSize(file.size)}</span>
            <button
              type="button"
              className="file-attachment-remove"
              onClick={() => removeQueuedFile(id)}
              title="Remove file"
              aria-label="Remove file"
              disabled={isUploadingAttachments}
            >
              x
            </button>
          </div>
        );
      })}
    </div>
  ) : null;

  const hiddenFileInput = (
    <input
      ref={fileInputRef}
      type="file"
      multiple
      style={{ display: 'none' }}
      onChange={handleFileInputChange}
    />
  );

  const dragOverlay = isDragOver ? (
    <div className="composer-drag-overlay">
      <p>Drop files to attach to this message</p>
    </div>
  ) : null;

  // Flags offered by the popover: feature-gated flags are hidden unless an
  // admin has enabled the matching server-global feature (Settings > Features).
  const visibleFlags = getVisibleFlags(enabledFeatures);

  const flagsPopover = isFlagsOpen ? (
    <div className="flags-popover">
      {visibleFlags.map((flag) => (
        <label key={flag.id} className="flags-popover-row">
          <input
            type="checkbox"
            checked={selectedFlags.includes(flag.id)}
            onChange={(e) => {
              setSelectedFlags((prev) =>
                e.target.checked
                  ? [...prev, flag.id]
                  : prev.filter((f) => f !== flag.id),
              );
            }}
          />
          <span className="flags-popover-text">
            <span className="flags-popover-name">{flag.label}</span>
            <span className="flags-popover-desc">{flag.description}</span>
          </span>
        </label>
      ))}
    </div>
  ) : null;

  const modals = (
    <>
      <SkillSelectorModal
        isOpen={isSkillModalOpen}
        onClose={() => setIsSkillModalOpen(false)}
        onConfirm={handleSkillConfirm}
        alreadyLoadedSkillIds={loadedSkills}
        autoloadedSkillIds={[]}
      />

      <SystemPromptModal
        isOpen={isSystemPromptModalOpen}
        conversationId={conversationId}
        onClose={() => setIsSystemPromptModalOpen(false)}
      />
    </>
  );

  // ----------------------------------------------------------------
  // Render: floating bubble (see module docstring)
  // ----------------------------------------------------------------

  // Post-first-message read-only flags label. Prefer the persisted flags
  // (hydrated from the GET response); fall back to the local selection while
  // the first send round-trips. Null when no flags are enabled.
  const effectiveFlags = conversationFlags.length > 0 ? conversationFlags : selectedFlags;
  const flagsLabel = !isFirstMessage && effectiveFlags.length > 0 ? (
    <span className="flags-label" title={effectiveFlags.map(getFlagLabel).join(', ')}>
      {effectiveFlags.length} flag{effectiveFlags.length === 1 ? '' : 's'} enabled
    </span>
  ) : null;

  const contextIndicator = (
    <ContextIndicator
      contextTokens={contextTokens}
      maxContextTokens={maxContextTokens}
      modelId={selectedModel}
      onInfoClick={() => setIsSystemPromptModalOpen(true)}
    />
  );

  // Read-only conversations (Slack-driven, inference runs) still show which
  // model they run on plus the context gauge -- disabled trigger, no send.
  // "Server default" sentinel when the conversation was created without an
  // explicit model (matches the Slack settings label); getModelDisplayName
  // falls back to the raw id for retired / non-registered models.
  const readOnlyControls = (
    <>
      <ModelSelector
        selectedModel={conversationModel ?? ''}
        models={getKnownModels()}
        onSelect={() => { /* disabled; no-op */ }}
        disabled
        labelOverride={conversationModel
          ? getModelDisplayName(conversationModel)
          : 'Server default'}
      />
      <div className="composer-spacer" />
      {contextIndicator}
    </>
  );

  const liveControls = (
    <>
      <button
        type="button"
        className="composer-icon-btn"
        onClick={handleAttachClick}
        disabled={inputDisabled}
        title="Attach files to this message"
        aria-label="Attach files"
      >
        <Paperclip size={18} />
        {pendingFiles.length > 0 && (
          <span className="composer-badge">{pendingFiles.length}</span>
        )}
      </button>
      {voiceInputEnabled && (
        <>
          <button
            type="button"
            className={
              'composer-icon-btn composer-mic'
              + (recorderStatus === 'recording' ? ' recording' : '')
              + (recorderStatus === 'starting' ? ' starting' : '')
            }
            onClick={handleMicClick}
            disabled={inputDisabled || isTranscribing || recorderStatus === 'starting'}
            aria-pressed={recorderStatus === 'recording'}
            title={
              isTranscribing
                ? 'Transcribing…'
                : recorderStatus === 'starting'
                  ? 'Starting the microphone…'
                  : recorderStatus === 'recording'
                    ? 'Stop recording'
                    : 'Dictate a message (transcribed on this server)'
            }
            aria-label={recorderStatus === 'recording' ? 'Stop recording' : 'Dictate a message'}
          >
            {isTranscribing ? (
              <LoaderCircle size={18} className="composer-mic-spinner" />
            ) : recorderStatus === 'recording' ? (
              <Square size={14} fill="currentColor" />
            ) : (
              <Mic size={18} />
            )}
          </button>
          {recorderStatus === 'recording' && (
            <>
              <span
                className="composer-mic-level"
                aria-hidden="true"
                style={{ '--mic-level': recordingLevel } as React.CSSProperties}
              >
                <i /><i /><i /><i />
              </span>
              <span className="composer-mic-timer" aria-live="polite">
                {formatRecordingTime(recordingSeconds)}
              </span>
            </>
          )}
        </>
      )}
      {!isPublicProject && (
        <button
          type="button"
          className="composer-icon-btn"
          onClick={() => setIsSkillModalOpen(true)}
          title="Load skills into this conversation"
          aria-label="Load skills"
        >
          <Sparkles size={18} />
          {queuedSkills.length > 0 && (
            <span className="composer-badge">{queuedSkills.length}</span>
          )}
        </button>
      )}
      {/* Flags lock at conversation start: interactive button + popover on a
          fresh conversation (hidden when feature gates leave no flag to
          offer), the read-only count label afterwards. */}
      {isFirstMessage && visibleFlags.length > 0 && (
        <div className="flags-container" ref={flagsMenuRef}>
          <button
            type="button"
            className="composer-icon-btn"
            onClick={() => setIsFlagsOpen((o) => !o)}
            disabled={inputDisabled}
            title="Enable per-conversation flags (set at the start)"
            aria-label="Flags"
          >
            <Flag size={18} />
            {selectedFlags.length > 0 && (
              <span className="composer-badge">{selectedFlags.length}</span>
            )}
          </button>
          {flagsPopover}
        </div>
      )}
      <ModelSelector
        selectedModel={selectedModel}
        models={selectableModels}
        onSelect={handleModelSelect}
        disabled={inputDisabled}
        visibility={modelVisibility}
        onOpenChange={setIsModelMenuOpen}
      />
      {noModelsAvailable && (
        <span
          className="composer-no-models"
          title="No LLM credentials configured -- sending is disabled. Configure a Vertex AI project in server_config.json (anthropic / gemini_vertex vertex_project_id)."
        >
          ⚠ No LLM credentials
        </span>
      )}
      {flagsLabel}
      <div className="composer-spacer" />
      {contextIndicator}
      {isStreaming && onStop ? (
        <button
          type="button"
          className="composer-send composer-stop"
          onClick={onStop}
          title="Stop"
          aria-label="Stop"
        >
          <Square size={14} fill="currentColor" />
        </button>
      ) : (
        <button
          type="button"
          className="composer-send"
          onClick={handleSendMessage}
          disabled={sendDisabled}
          title={isUploadingAttachments ? 'Sending…' : isRecording ? 'Stop recording and send' : 'Send'}
          aria-label={isRecording ? 'Stop recording and send' : 'Send'}
        >
          <ArrowUp size={18} />
        </button>
      )}
    </>
  );

  return (
    <>
      <div
        className={
          'input-area composer'
          + (expanded ? ' composer-expanded' : '')
          + (isDragOver ? ' drag-over' : '')
        }
        onFocus={handleRootFocus}
        onBlur={handleRootBlur}
        onDragEnter={handleDragEnter}
        onDragOver={handleDragOver}
        onDragLeave={handleDragLeave}
        onDrop={handleDrop}
      >
        {dragOverlay}
        {publicProjectBanner}
        {readOnlyBanner}
        {pasteNoticeEl}
        {hiddenFileInput}
        <div className="composer-bubble">
          {pastedImagesRow}
          {queuedFilesRow}
          <textarea
            ref={inputRef}
            className="message-input composer-input"
            value={inputValue}
            onChange={handleInputChange}
            onKeyDown={handleKeyDown}
            onPaste={inputDisabled ? undefined : handlePaste}
            placeholder={composerPlaceholder}
            disabled={inputDisabled}
            rows={1}
          />
          {expanded && (
            <div
              className="composer-controls"
              // Phone only: keep the textarea focused when tapping a control.
              // Without this the tap blurs the textarea, the collapse unmounts
              // the row mid-tap, and the click never lands (iOS Safari). The
              // desktop row never collapses, so let controls take focus there.
              onMouseDown={isMobile ? (e) => e.preventDefault() : undefined}
            >
              {isReadOnly ? readOnlyControls : liveControls}
            </div>
          )}
        </div>
      </div>
      {modals}
    </>
  );
}
