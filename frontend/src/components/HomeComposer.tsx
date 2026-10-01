/**
 * HomeComposer -- the root empty-state ("/" with no conversation) home screen.
 *
 * Renders a large greeting + the shared <Composer> (model selector, Skill
 * button, Flags popover) centered in the main content
 * area. There is no live conversation yet: the composer is bound to a stable
 * in-memory DRAFT KEY used only for the context-keyed model/skills lookups.
 *
 * On submit it runs a deferred-create flow: create the conversation, seed the
 * store for an instant paint, carry the home-chosen model/skills/flags onto the
 * real conversation (queued skills re-keyed; model/skills/flags also stashed in
 * the ``pendingFirstMessage`` context field), then navigate to /chats/<id>.
 * ChatPanel's pendingFirstMessage auto-send effect performs the real first send.
 *
 * Project awareness: when the Sidebar is drilled into a project (context
 * ``drilledProjectId``; e.g. right after creating a new empty project, which
 * keeps the URL at "/" and this composer on screen), the first send creates a
 * conversation INSIDE that project and navigates to /projects/<pid>/<id>, so
 * typing here matches what the left nav shows. The greeting names the project
 * as the visible cue.
 *
 * Private / public model contexts: the per-user "last-used" model is tracked
 * separately for private conversations and for public-project conversations
 * (the admin's allow-lists differ per visibility -- see model-selection.md).
 * The composer stays mounted while the user drills into or out of a public
 * project, so whenever the drilled project's ``public`` flag changes we
 * re-resolve the last-used model for the new context and re-seed the draft
 * selection with it, instead of holding on to a pick that may not be allowed
 * in the new context.
 *
 * Generic file attachments queued in the composer are uploaded here, after
 * createConversation() resolves (the workspace only exists then) and before
 * navigation, via POST /files/upload. Their workspace-relative names are stashed
 * onto pendingFirstMessage.attachedFilenames so the first turn's metadata can
 * name them. (The image multimodal path remains disabled on the home screen.)
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { useAppConfig } from '../contexts/AppConfigContext';
import { useAuth } from '../contexts/AuthContext';
import { useConversationModels } from '../contexts/ConversationModelsContext';
import { useConversationSkills } from '../contexts/ConversationSkillsContext';
import { useNavigationState } from '../contexts/NavigationContext';
import { useProjects } from '../contexts/ProjectsContext';
import { createConversation, createProjectConversation, ApiClientError } from '../api/client';
import { uploadComposerAttachments, uploadFiles } from '../api/fileApi';
import { seedNewConversation } from '../utils/newConversation';
import type { ComposerAttachmentRef } from '../api/types';
import { HOME_DRAFT_KEY } from '../constants/drafts';
import type { ModelVisibility } from '../constants/models';
import { Composer } from './Composer';
import { HeartBoltMorph } from './HeartBoltMorph';
import './HomeComposer.css';

interface HomeComposerProps {
  /**
   * Create+navigate callback. Wired to AppContent.handleNewConversation so the
   * home screen reuses the exact same navigation path as the Sidebar "New
   * Chat" button. Called with (newConversationId, null) for a standalone chat,
   * or (newConversationId, projectId) when the Sidebar is drilled into a
   * project and the chat was created inside it.
   */
  onNewConversation: (id: string, projectId?: string | null) => void;
}

export function HomeComposer({ onNewConversation }: HomeComposerProps) {
  const { userName } = useAuth();
  const { appName } = useAppConfig();
  const {
    getModelForConversation,
    setDraftModelForConversation,
    refreshDefaultModel,
  } = useConversationModels();
  const {
    getQueuedSkillsForConversation,
    setQueuedSkillsForConversation,
    setLoadedSkillsForConversation,
  } = useConversationSkills();
  const { setPendingFirstMessage } = useNavigationState();
  const { setActiveProjectId, drilledProjectId, projects } = useProjects();

  // When the Sidebar is drilled into a project, the first send targets that
  // project; name it under the greeting so the destination is visible.
  const drilledProject = drilledProjectId
    ? projects.find((p) => p.id === drilledProjectId) ?? null
    : null;
  const drilledProjectName = drilledProject?.name ?? null;
  // A public project's first chat starts here too: the composer needs to
  // know so it offers only public-allowed models (and shows the
  // sensitive-info banner / hides the Skill button like ChatPanel does).
  const isPublicProject = Boolean(drilledProject?.public);
  const modelVisibility: ModelVisibility = isPublicProject ? 'public' : 'private';

  // Always-fresh fetch on mount AND on every private<->public context switch
  // (drilling into / out of a public project keeps this composer mounted):
  // re-read the per-user last-used model for the current context from the
  // server (GET /me) so the home composer reflects the latest server value
  // regardless of what other tabs did, then re-seed the draft-keyed model
  // entry so it does not shadow the refreshed default. A switch while a
  // refresh is in flight cancels the stale one. No localStorage, no WS push.
  useEffect(() => {
    let cancelled = false;
    refreshDefaultModel(modelVisibility).then((resolved) => {
      if (cancelled) return;
      // Re-key the draft entry to the freshly-resolved server default so
      // getModelForConversation(HOME_DRAFT_KEY) returns it rather than a stale
      // draft selection left over from a previous mount / the other context.
      setDraftModelForConversation(HOME_DRAFT_KEY, resolved, modelVisibility);
    });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [modelVisibility]);

  // Guard against double-submit while a createConversation() is in flight.
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const submittingRef = useRef(false);

  // Greeting: first name only; drop the name entirely when it's missing/blank.
  const firstName = userName?.trim().split(/\s+/)[0];
  const greeting = firstName
    ? `What can I help you with, ${firstName}?`
    : 'What can I help you with?';

  // The shared composer's onSend. The text/model/skills/flags come from the
  // composer's own state + the draft-keyed context maps. ``attachments`` (the
  // already-uploaded image refs) is always undefined here: the composer runs
  // in ``deferImageUpload`` mode because its upload endpoint needs a real
  // conversation id, which doesn't exist for the draft key. Instead the raw
  // pasted images arrive as ``imageFiles`` and, like generic ``files``, are
  // uploaded only after ``createConversation()`` resolves; the returned refs
  // ride ``pendingFirstMessage.attachments`` into the first send and the file
  // names ride ``pendingFirstMessage.attachedFilenames`` so the first turn's
  // metadata can name them. We ignore the model arg passed by the composer
  // and re-read the draft-keyed model to be explicit.
  const handleSend = useCallback(
    (
      text: string,
      model: string,
      skillIds?: string[],
      _attachments?: ComposerAttachmentRef[],
      flags?: string[],
      files?: File[],
      imageFiles?: File[],
    ) => {
      const trimmed = text.trim();
      const hasImages = !!imageFiles && imageFiles.length > 0;
      if ((!trimmed && !hasImages) || submittingRef.current) return;
      submittingRef.current = true;
      setSubmitting(true);
      setError(null);

      // Capture the drilled project at submit time so a mid-flight drill
      // in/out can't split the create target from the navigation target.
      const projectId = drilledProjectId;
      const createPromise = projectId
        ? createProjectConversation(projectId)
        : createConversation();

      createPromise
        .then(async (response) => {
          const newId = response.id;

          // Generic files: the workspace exists now, so upload them BEFORE
          // navigating / stashing the first message. The returned names ride
          // pendingFirstMessage so the first turn's metadata lists them. On a
          // hard failure, surface the error and do NOT proceed (the user keeps
          // their text and can retry); partial failures proceed with a notice.
          let attachedFilenames: string[] = [];
          if (files && files.length > 0) {
            const uploadResp = await uploadFiles(newId, files, '');
            attachedFilenames = uploadResp.uploadedFiles.map((f) => f.name);
            if (uploadResp.errors && uploadResp.errors.length > 0) {
              setError(
                `Some files could not be uploaded: ${uploadResp.errors
                  .map((e) => e.message)
                  .join('; ')}`,
              );
            }
          }

          // Pasted images: same deferral as generic files, but through the
          // stricter composer-attachments endpoint so the refs can be sent as
          // multimodal attachments on the first turn. If every image is
          // rejected, abort like a hard failure so the text isn't sent alone.
          let attachments: ComposerAttachmentRef[] = [];
          if (hasImages) {
            const imgResp = await uploadComposerAttachments(newId, imageFiles);
            attachments = imgResp.attachments ?? [];
            if (imgResp.errors && imgResp.errors.length > 0) {
              const detail = imgResp.errors.map((e) => e.message).join('; ');
              if (attachments.length === 0) {
                throw new Error(`Pasted images could not be uploaded: ${detail}`);
              }
              setError(`Some pasted images could not be uploaded: ${detail}`);
            }
          }

          // Seed the store so ChatPanel paints the empty composer immediately.
          seedNewConversation(newId, setLoadedSkillsForConversation);

          // Carry the home-chosen selections onto the real conversation BEFORE
          // navigating, so ChatPanel's auto-send effect sees a fully-populated
          // state on first mount.
          const effectiveModel = model || getModelForConversation(HOME_DRAFT_KEY);
          const queuedSkills = skillIds && skillIds.length > 0
            ? skillIds
            : getQueuedSkillsForConversation(HOME_DRAFT_KEY);
          const effectiveSkills = queuedSkills.length > 0 ? queuedSkills : [];

          // Re-key the queued skills onto the real conversation id.
          if (effectiveSkills.length > 0) {
            setQueuedSkillsForConversation(newId, effectiveSkills);
          }
          // Clear the draft-keyed leftover so it doesn't bleed into a future
          // home-screen mount.
          setQueuedSkillsForConversation(HOME_DRAFT_KEY, []);

          // Stash the first message (+ carried selections) for the ChatPanel
          // auto-send effect.
          setPendingFirstMessage({
            conversationId: newId,
            prompt: trimmed,
            model: effectiveModel,
            skillIds: effectiveSkills,
            flags: flags ?? [],
            isPublicProject,
            attachedFilenames,
            attachments,
          });

          // Match the Sidebar's create paths: project chats keep the drilled
          // project active (handleNewProjectChat), standalone chats clear it
          // (handleNewChat).
          setActiveProjectId(projectId);

          // Navigate -- same path as the Sidebar "New Chat" / project "+" buttons.
          onNewConversation(newId, projectId);
        })
        .catch((err) => {
          // Surface inline; keep the typed text so the user can retry (the
          // composer cleared its input optimistically, but the create/upload
          // failed, so we re-surface via the error and the user re-types).
          // Mirrors the Sidebar new-chat error handling. An upload failure
          // here lands in the same path: we do not navigate / send.
          if (err instanceof ApiClientError) {
            setError(err.message);
          } else if (err instanceof Error) {
            setError(err.message);
          } else {
            setError('Failed to create conversation');
          }
        })
        .finally(() => {
          submittingRef.current = false;
          setSubmitting(false);
        });
    },
    [
      drilledProjectId,
      isPublicProject,
      getModelForConversation,
      getQueuedSkillsForConversation,
      setQueuedSkillsForConversation,
      setLoadedSkillsForConversation,
      setPendingFirstMessage,
      setActiveProjectId,
      onNewConversation,
    ],
  );

  return (
    <div className="home-composer">
      <div className="home-composer-body">
        <div className="home-composer-inner">
          <h1 className="home-composer-greeting">{greeting}</h1>
          {drilledProjectName && (
            <p className="home-composer-project-hint">
              New chat in <strong>{drilledProjectName}</strong>
            </p>
          )}
          {error && (
            <div className="home-composer-error" role="alert">{error}</div>
          )}
          <div className="home-composer-composer">
            <Composer
              conversationId={HOME_DRAFT_KEY}
              onSend={handleSend}
              isFirstMessage
              isStreaming={submitting}
              skipSendLocks
              deferImageUpload
              isPublicProject={isPublicProject}
              onModelChange={(m) => setDraftModelForConversation(HOME_DRAFT_KEY, m, modelVisibility)}
            />
          </div>
        </div>
      </div>
      {/* Branding footer pinned to the bottom of the home screen. The heart
          is a monochrome currentColor glyph (not a colour emoji) that morphs
          into the Electric lightning bolt and back -- see HeartBoltMorph. */}
      <footer className="home-composer-footer">
        <p className="home-composer-footer-line">
          Remember AI can make mistakes, but {appName} keeps you in control.
        </p>
        <p className="home-composer-footer-line">
          Made with <HeartBoltMorph className="home-composer-footer-heart" size={18} /> by Electric Capital
        </p>
      </footer>
    </div>
  );
}
