/**
 * TaskComposer: the shared <Composer> set up for Quest Tasks, used by the
 * New Task page and the task thread.
 *
 * A task composer never sends on the conversation it is bound to: every
 * Send starts a run on a fresh run conversation (services/taskRuns.ts), so
 * the composer is bound to a stable draft key (constants/drafts.ts:
 * TASK_DRAFT_KEY on the New Task page, taskDraftKey(taskId) in a thread)
 * that only keys the model and skills lookups, exactly like the home
 * screen's HOME_DRAFT_KEY:
 *
 * - `skipSendLocks`: a draft key must never get a provider lock (each run is
 *   a new conversation, so the family may change from run to run);
 * - `deferImageUpload`: pasted images reach the host as raw files, uploaded
 *   once the run conversation exists (startTaskRunFromComposer);
 * - `hideFlags`: task runs take no conversation flags;
 * - the model is seeded on mount (and on a draft key change) with the
 *   `initialModel` when given (a thread passes the task's model), else the
 *   user's last-used private model from a fresh GET /me, and picks are
 *   draft-safe (setDraftModelForConversation: no conversation PATCH). A
 *   user's pick in the selector is then reported to `onModelChange` (the
 *   thread persists it as the task's default model, the New Task page
 *   passes it to the create); the seed never is;
 * - a draft key never keeps "loaded" skills (each run starts from a fresh
 *   context), so the skill picker always offers every skill again.
 *
 * Send builds a TaskComposerSubmission and hands it to `onSubmit`; the host
 * creates what it needs and calls startTaskRunFromComposer. The composer
 * clears its text and attachments as Send is pressed and cannot be
 * re-seeded, so a failed start loses the typed text (as on the home
 * screen); the host shows the error.
 *
 * Lock states: `busy` (a submit is in flight) and `disabled` (e.g. a run
 * is working) both lock the input; with `disabled` and `onStop` the Send
 * button becomes Stop. `placeholder` replaces the default input text.
 */

import { useCallback, useEffect } from 'react';
import type { ComposerAttachmentRef } from '../../api/types';
import { useConversationModels } from '../../contexts/ConversationModelsContext';
import { useConversationSkills } from '../../contexts/ConversationSkillsContext';
import { Composer } from '../Composer';
import type { TaskComposerSubmission } from './taskComposerSend';

export interface TaskComposerProps {
  /** The draft key (TASK_DRAFT_KEY, or taskDraftKey(taskId) in a thread). */
  draftKey: string;
  onSubmit: (submission: TaskComposerSubmission) => void;
  /** A submit is in flight: the input is locked, no Stop. */
  busy?: boolean;
  /** The input is locked for another reason; Stop shows when `onStop` is given. */
  disabled?: boolean;
  /** Called by the Stop button, shown only while `disabled`. */
  onStop?: () => void;
  /** Replaces the default input placeholder. */
  placeholder?: string;
  /** Seed for the model pick (e.g. the task's model); else the user's last-used model. */
  initialModel?: string | null;
  /** Thinking level seeded with `initialModel`. */
  initialThinkingEffort?: string | null;
  /** A model / thinking level the user picked in the selector (after the draft pick is applied). */
  onModelChange?: (model: string, thinkingEffort: string | null) => void;
}

export function TaskComposer({
  draftKey,
  onSubmit,
  busy = false,
  disabled = false,
  onStop,
  placeholder,
  initialModel = null,
  initialThinkingEffort = null,
  onModelChange,
}: TaskComposerProps) {
  const {
    getModelForConversation,
    getThinkingEffortForConversation,
    setDraftModelForConversation,
    refreshDefaultModel,
  } = useConversationModels();
  const {
    getQueuedSkillsForConversation,
    getLoadedSkillsForConversation,
    setLoadedSkillsForConversation,
  } = useConversationSkills();

  // Seed the draft's model on mount and whenever the draft key changes:
  // the given model, else the freshly-read last-used private model (task
  // runs are private conversations).
  useEffect(() => {
    if (initialModel) {
      setDraftModelForConversation(draftKey, initialModel, 'private', initialThinkingEffort);
      return;
    }
    let cancelled = false;
    refreshDefaultModel('private').then((resolved) => {
      if (cancelled) return;
      setDraftModelForConversation(draftKey, resolved.model, 'private', resolved.thinkingEffort);
    });
    return () => {
      cancelled = true;
    };
    // Seeded once per draft key; a later initialModel change must not
    // overwrite the user's pick.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [draftKey]);

  // The composer marks sent skills as loaded on its draft key; a run starts
  // from a fresh context, so drop them again.
  const loadedSkills = getLoadedSkillsForConversation(draftKey);
  useEffect(() => {
    if (loadedSkills.length > 0) setLoadedSkillsForConversation(draftKey, []);
  }, [draftKey, loadedSkills, setLoadedSkillsForConversation]);

  const handleSend = useCallback(
    (
      text: string,
      model: string,
      skillIds?: string[],
      _attachments?: ComposerAttachmentRef[],
      _flags?: string[],
      files?: File[],
      imageFiles?: File[],
      thinkingEffort: string | null = null,
    ) => {
      const queued = skillIds && skillIds.length > 0
        ? skillIds
        : getQueuedSkillsForConversation(draftKey);
      onSubmit({
        text: text.trim(),
        model: model || getModelForConversation(draftKey) || null,
        thinkingEffort: model ? thinkingEffort : getThinkingEffortForConversation(draftKey),
        skillIds: queued,
        files: files ?? [],
        imageFiles: imageFiles ?? [],
      });
    },
    [
      draftKey,
      onSubmit,
      getModelForConversation,
      getThinkingEffortForConversation,
      getQueuedSkillsForConversation,
    ],
  );

  // Only the selector calls this (the seed above goes straight to the
  // context), so the host hears user picks only.
  const handleModelChange = useCallback(
    (model: string, thinkingEffort: string | null) => {
      setDraftModelForConversation(draftKey, model, 'private', thinkingEffort);
      onModelChange?.(model, thinkingEffort);
    },
    [draftKey, onModelChange, setDraftModelForConversation],
  );

  return (
    <Composer
      conversationId={draftKey}
      onSend={handleSend}
      onStop={disabled ? onStop : undefined}
      isFirstMessage
      isStreaming={busy || disabled}
      skipSendLocks
      deferImageUpload
      hideFlags
      placeholder={placeholder}
      onModelChange={handleModelChange}
    />
  );
}
