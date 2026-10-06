/**
 * Sidebar shell: brand bar, the two sliding panels (projects + docs +
 * standalone conversations, and the drilled project), the update banner, the
 * user bar and the modals. Owns the cross-list flows -- drilling in and out
 * of a project, creating chats, running routines, opening docs,
 * project/routine CRUD follow-ups -- and delegates:
 *
 * - data fetching / paging / polling / realtime refresh to
 *   hooks/useTopLevelConversations, useProjectConversations,
 *   useProjectRoutines, useDocs
 * - list derivation (filters, routine grouping, ordering) to
 *   utils/sidebarItems and utils/sidebarDocs
 * - section rendering to components/sidebar/*
 */

import React, { useEffect, useState, useRef } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import { Search, Inbox, SquarePen } from 'lucide-react';
import {
  duplicateConversationWorkspace,
  createProjectConversation,
  ApiClientError,
} from '../api/client';
import { DOCS_FEATURE } from '../api/docsApi';
import type { Conversation, Routine } from '../api/types';
import { useAppConfig } from '../contexts/AppConfigContext';
import { useAuth } from '../contexts/AuthContext';
import { useConversationModels } from '../contexts/ConversationModelsContext';
import { useConversationSkills } from '../contexts/ConversationSkillsContext';
import { useNavigationState } from '../contexts/NavigationContext';
import { useProjects } from '../contexts/ProjectsContext';
import { UserInfoBar } from './UserInfoBar';
import { NewProjectModal } from './NewProjectModal';
import { ConvertToProjectModal } from './ConvertToProjectModal';
import { ProjectSettingsModal } from './ProjectSettingsModal';
import { NewRoutineModal } from './NewRoutineModal';
import { RoutineSettingsModal } from './RoutineSettingsModal';
import { DEPRECATED_MODEL_MAP } from '../constants/models';
import { seedNewConversation } from '../utils/newConversation';
import { useTopLevelConversations } from '../hooks/useTopLevelConversations';
import { useProjectConversations } from '../hooks/useProjectConversations';
import { useProjectRoutines } from '../hooks/useProjectRoutines';
import { useDocs } from '../hooks/useDocs';
import { docsListPath, docViewerPath, parseDocsRoute } from '../utils/docsRoute';
import { SIDEBAR_DOC_LIMIT } from '../utils/sidebarDocs';
import { SearchModal } from './SearchModal';
import { QuestLogo } from './QuestLogo';
import { RequestsBadge } from './sidebar/RequestsBadge';
import { ProjectsSection } from './sidebar/ProjectsSection';
import { DocsSection } from './sidebar/DocsSection';
import { ConversationsSection } from './sidebar/ConversationsSection';
import { ProjectPanel } from './sidebar/ProjectPanel';
import './Sidebar.css';

// Server-global feature gate key for routines in public projects
// (config/feature_gates.py FEATURE_PUBLIC_PROJECT_ROUTINES).
const PUBLIC_PROJECT_ROUTINES_FEATURE = 'public_project_routines';

interface SidebarProps {
  activeConversationId: string | null;
  // opts.implicit marks selections that are side effects of drilling in/out of
  // a project (keeping the main pane in sync) rather than the user picking a
  // conversation. The mobile shell keeps its nav drawer open for those.
  onConversationSelect: (id: string, projectId?: string | null, opts?: { implicit?: boolean }) => void;
  onNewConversation: (id: string, projectId?: string | null) => void;
  // Called after the Sidebar navigates to a view that is not a conversation
  // (a doc, the All Docs list): the host closes its drawer on phone. The
  // desktop layout passes nothing.
  onNavigateAway?: () => void;
}

/**
 * Sidebar component wrapped in React.memo to prevent re-renders when
 * parent re-renders with the same props. Combined with useCallback on the
 * callback props in App.tsx, this ensures the Sidebar only re-renders
 * when its own props or internal state actually change.
 */
export const Sidebar = React.memo(function Sidebar({
  activeConversationId,
  onConversationSelect,
  onNewConversation,
  onNavigateAway,
}: SidebarProps) {
  const { userEmail, userName, enabledFeatures } = useAuth();
  const { appName, updateAvailable } = useAppConfig();
  const {
    setSettingsOpen,
    showRequestsView,
    setShowRequestsView,
    setScrollToMessageIndex,
    setPendingRoutineMessage,
  } = useNavigationState();
  const {
    projects,
    projectsLoaded,
    loadProjects,
    activeProjectId,
    setActiveProjectId,
    drilledProjectId,
    setDrilledProjectId,
  } = useProjects();
  const { setModelForConversation } = useConversationModels();
  const { setLoadedSkillsForConversation } = useConversationSkills();

  // Public projects get routines only while an admin has the
  // public_project_routines feature gate open for this user (the routine
  // API and the scheduler enforce the same gate).
  const publicRoutinesEnabled = enabledFeatures.includes(PUBLIC_PROJECT_ROUTINES_FEATURE);
  const drilledProject = projects.find((p) => p.id === drilledProjectId) ?? null;
  // The routine modals trim themselves for a public project (public model
  // list, no skill auto-loads or guide override).
  const drilledProjectIsPublic = Boolean(drilledProject?.public);

  const navigate = useNavigate();

  // Quest Docs: the user's latest docs on the main panel and the drilled
  // project's in its panel, both only while the feature gate is open. The URL
  // says which doc / All Docs view is showing (no NavigationContext state).
  const docsEnabled = enabledFeatures.includes(DOCS_FEATURE);
  const userDocs = useDocs({ limit: SIDEBAR_DOC_LIMIT, enabled: docsEnabled });
  const projectDocs = useDocs({
    projectId: drilledProjectId,
    limit: SIDEBAR_DOC_LIMIT,
    enabled: docsEnabled && drilledProjectId !== null,
  });
  const location = useLocation();
  const docsRoute = parseDocsRoute(location.pathname, location.search);
  const activeDocId = docsRoute?.kind === 'viewer' ? docsRoute.docId : null;
  // The project id of the All Docs view showing (null = unfiltered), or
  // undefined when no All Docs view is showing.
  const docsListProjectId = docsRoute?.kind === 'list' ? docsRoute.projectId : undefined;

  // Per-project conversation filters. Default to false (hide archived, hide
  // Slack conversations) on every page load; no persistence. Independent of
  // the top-level section's filters (which live in its data hook).
  const [showArchivedProject, setShowArchivedProject] = useState(false);
  const [showSlackProject, setShowSlackProject] = useState(false);
  // The Projects section's own "Show Archived" (archived PROJECTS, as
  // opposed to archived conversations inside a project). Same lifetime.
  const [showArchivedProjects, setShowArchivedProjects] = useState(false);

  const topLevel = useTopLevelConversations();
  const projectConversations = useProjectConversations(drilledProjectId, showArchivedProject);
  const projectRoutines = useProjectRoutines(drilledProjectId);
  // The sidebar's one error banner: list load errors and action errors share it.
  const { error, setError } = topLevel;

  // In-flight creation flags (disable the New Chat buttons meanwhile).
  const [creating, setCreating] = useState<boolean>(false);
  const [creatingInProject, setCreatingInProject] = useState<string | null>(null);

  // Drill-down state. drilledProjectId lives in ProjectsContext (not local
  // state) so the root HomeComposer can target new chats at the drilled
  // project; the slide animation state stays local.
  const [slideDirection, setSlideDirection] = useState<'none' | 'left' | 'right'>('none');

  // Routine conversation group expansion state
  const [expandedRoutineGroups, setExpandedRoutineGroups] = useState<Record<string, boolean>>({});

  // Remember the top-level conversation selected before drilling into a project
  const previousTopLevelConversationId = useRef<string | null>(null);

  // Search modal state
  const [showSearchModal, setShowSearchModal] = useState(false);

  // Modals
  const [showNewProjectModal, setShowNewProjectModal] = useState(false);
  const [convertConversation, setConvertConversation] =
    useState<{ id: string; title: string } | null>(null);
  const [settingsProjectId, setSettingsProjectId] = useState<string | null>(null);
  const [showNewRoutineModal, setShowNewRoutineModal] = useState(false);
  const [settingsRoutine, setSettingsRoutine] = useState<Routine | null>(null);

  // Global keyboard shortcut: Cmd/Ctrl+K opens search modal
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key === 'k') {
        e.preventDefault();
        setShowSearchModal(prev => !prev);
      }
    };
    document.addEventListener('keydown', handleKeyDown);
    return () => document.removeEventListener('keydown', handleKeyDown);
  }, []);

  // When activeProjectId is set externally, auto drill-down to that project
  useEffect(() => {
    if (activeProjectId && activeProjectId !== drilledProjectId) {
      handleProjectDrillDown(activeProjectId);
    }
  }, [activeProjectId]);

  function toggleRoutineGroup(routineId: string) {
    setExpandedRoutineGroups(prev => ({
      ...prev,
      [routineId]: !prev[routineId],
    }));
  }

  async function handleProjectDrillDown(projectId: string) {
    // Save the current top-level conversation so we can restore it on back-nav
    if (!drilledProjectId) {
      previousTopLevelConversationId.current = activeConversationId || null;
    }
    // Routines load from useProjectRoutines once drilledProjectId changes.
    setSlideDirection('left');
    setDrilledProjectId(projectId);
    setActiveProjectId(projectId);

    // Determine the project's conversations (use cache or fetch)
    let convos: Conversation[];
    if (projectConversations.byProject[projectId]) {
      convos = projectConversations.byProject[projectId];
    } else {
      // Only clear current conversation while loading if we don't already
      // have a URL-driven conversationId (i.e., the URL is the source of truth)
      if (!activeConversationId) {
        onConversationSelect('', null, { implicit: true });
      }
      convos = await projectConversations.load(projectId);
    }

    // If the currently active conversation (e.g. from the URL) belongs to
    // this project, keep it selected instead of clobbering it with the first
    // conversation.  The URL is the source of truth.
    if (activeConversationId && convos.some((c) => c.id === activeConversationId)) {
      onConversationSelect(activeConversationId, projectId, { implicit: true });
    } else if (convos.length > 0) {
      // Auto-select the latest conversation when no specific one is requested
      onConversationSelect(convos[0].id, projectId, { implicit: true });
    } else {
      onConversationSelect('', null, { implicit: true });
    }
  }

  function handleDrillDownBack() {
    setSlideDirection('right');
    // Restore the top-level conversation that was active before drilling in,
    // or show the empty state if none was selected
    const savedId = previousTopLevelConversationId.current;
    previousTopLevelConversationId.current = null;
    onConversationSelect(savedId ?? '', null, { implicit: true });
    // Use a short timeout to let the animation play before clearing state
    setTimeout(() => {
      setDrilledProjectId(null);
      setSlideDirection('none');
    }, 200); // match CSS transition duration
  }

  // Seed the conversation store and adjacent caches for a chat we just
  // created on this tab (see utils/newConversation.ts for the rationale).
  // Shared with the root HomeComposer so both new-chat paths seed identically.
  function seedConversation(conversationId: string) {
    seedNewConversation(conversationId, setLoadedSkillsForConversation);
  }

  // "New Chat" (top-level Conversations "+", and the brand bar when not
  // drilled): nothing is created here. Navigate to the root home composer,
  // which creates the conversation as a response to the FIRST message (see
  // HomeComposer.tsx). Only reachable while not drilled into a project, so
  // the home composer targets a standalone chat.
  function handleNewChat() {
    setError(null);
    setShowRequestsView(false);
    setActiveProjectId(null);
    onNewConversation('', null);
  }

  // Brand-bar "new chat" pencil. While drilled into a project it must target
  // that project (same path as the drill-down panel's "+"), otherwise the
  // user lands in a standalone chat that isn't in the list they're looking at.
  function handleBrandBarNewChat() {
    if (drilledProjectId) {
      handleNewProjectChat(drilledProjectId);
    } else {
      handleNewChat();
    }
  }

  async function handleDuplicateWorkspace(sourceConversationId: string) {
    try {
      setCreating(true);
      setError(null);
      setShowRequestsView(false);
      const response = await duplicateConversationWorkspace(sourceConversationId);
      // Same optimistic seed + fire-and-forget refresh as handleNewChat: the
      // new conversation is empty, only its workspace files differ.
      seedConversation(response.id);
      void topLevel.refreshSilently();
      setActiveProjectId(null);
      onNewConversation(response.id, null);
    } catch (err) {
      if (err instanceof ApiClientError) {
        setError(err.message);
      } else {
        setError('Failed to duplicate workspace');
      }
    } finally {
      setCreating(false);
    }
  }

  // Project "+" New Chat: same deferred-create flow as handleNewChat, but the
  // Sidebar stays drilled into the project so the home composer creates the
  // chat INSIDE it on first send ("New chat in <project>" hint).
  function handleNewProjectChat(projectId: string) {
    setError(null);
    setShowRequestsView(false);
    setActiveProjectId(projectId);
    onNewConversation('', projectId);
  }

  function handleConversationClick(id: string, projectId?: string | null) {
    setShowRequestsView(false);
    if (projectId) {
      setActiveProjectId(projectId);
    } else {
      setActiveProjectId(null);
    }
    onConversationSelect(id, projectId);
  }

  // Docs views are URL routes (/docs/<id>, /docs[?project=<id>]); App maps
  // them to the main pane, where activeConversationId is null, so no
  // conversation row stays highlighted. Clearing the requests flag here
  // (like handleConversationClick) avoids a frame of the inbox in between.
  function handleOpenDoc(id: string) {
    setError(null);
    setShowRequestsView(false);
    navigate(docViewerPath(id));
    onNavigateAway?.();
  }

  function handleOpenAllDocs(projectId: string | null) {
    setError(null);
    setShowRequestsView(false);
    navigate(docsListPath(projectId));
    onNavigateAway?.();
  }

  // Deselect an archived conversation if it was the active one.
  function handleArchived(conversationId: string) {
    if (activeConversationId === conversationId) {
      onConversationSelect('', null);
    }
  }

  async function handleRunRoutine(projectId: string, routine: Routine) {
    try {
      setCreatingInProject(projectId);
      setError(null);

      // 1. Create a new conversation in the project, linked to the routine
      const response = await createProjectConversation(projectId, routine.id);
      seedConversation(response.id);
      // Fire-and-forget refresh of the project conversation list.
      void projectConversations.load(projectId, /* silent */ true);
      setActiveProjectId(projectId);

      // 2. Select the conversation and notify parent
      onNewConversation(response.id, projectId);

      // 3. Set the model for this conversation if the routine specifies one
      if (routine.model) {
        const effectiveModel = DEPRECATED_MODEL_MAP[routine.model] || routine.model;
        setModelForConversation(response.id, effectiveModel);
      }

      // 4. Auto-expand the routine group so the new conversation is visible
      setExpandedRoutineGroups(prev => ({ ...prev, [routine.id]: true }));

      // 5. Set the pending routine message so ChatPanel auto-sends the prompt
      setPendingRoutineMessage({
        conversationId: response.id,
        prompt: routine.prompt,
        guideId: routine.guide_id,
      });
    } catch (err) {
      if (err instanceof ApiClientError) {
        setError(err.message);
      } else {
        setError('Failed to run routine');
      }
    } finally {
      setCreatingInProject(null);
    }
  }

  function handleProjectCreated(projectId: string) {
    loadProjects();
    // Initialize conversations for the new project and drill down into it
    projectConversations.seedEmpty(projectId);
    handleProjectDrillDown(projectId);
  }

  function handleConvertedToProject(projectId: string, conversationId: string) {
    loadProjects();
    // The conversation now belongs to the project; drop it from the
    // top-level list immediately rather than waiting for the WS refetch.
    topLevel.updateConversations((prev) => prev.filter((c) => c.id !== conversationId));
    // Drill into the new project. The conversation list isn't cached yet, so
    // the drill-down fetches it fresh and auto-selects the moved conversation
    // (it's the only one in the project).
    handleProjectDrillDown(projectId);
  }

  function handleProjectUpdated() {
    loadProjects();
    // Reload routines for the current project in case they were added/edited/deleted
    if (drilledProjectId) {
      void projectRoutines.load(drilledProjectId);
    }
  }

  function handleProjectDeleted() {
    loadProjects();
    if (settingsProjectId === drilledProjectId) {
      handleDrillDownBack();
    }
    if (settingsProjectId === activeProjectId) {
      setActiveProjectId(null);
    }
    setSettingsProjectId(null);
  }

  // Routine create / update / delete from the modals: re-fetch the drilled
  // project's routine list.
  function handleRoutinesChanged() {
    if (drilledProjectId) {
      void projectRoutines.load(drilledProjectId);
    }
  }

  // No row is active while the requests inbox view is showing. (Docs views
  // need no check: activeConversationId is already null on their routes.)
  const highlightedConversationId = showRequestsView ? null : activeConversationId;

  return (
    <div className="sidebar">
      {error && (
        <div className="sidebar-error">
          {error}
        </div>
      )}

      <div className="sidebar-content">
        {/* Brand bar: logo + app name on the left, new chat + search + requests inbox on the right */}
        <div className="sidebar-brand">
          <div className="sidebar-brand-identity">
            <QuestLogo className="sidebar-brand-logo" />
            <span className="sidebar-brand-name">{appName}</span>
          </div>
          <div className="sidebar-brand-actions">
            <button
              className="sidebar-icon-button"
              onClick={handleBrandBarNewChat}
              disabled={creating || (drilledProjectId !== null && creatingInProject === drilledProjectId)}
              title={drilledProjectId ? 'New chat in this project' : 'New Chat'}
              aria-label={drilledProjectId ? 'New chat in this project' : 'New Chat'}
            >
              <SquarePen size={18} />
            </button>
            <button
              className="sidebar-icon-button"
              onClick={() => setShowSearchModal(true)}
              title="Search conversations"
              aria-label="Search conversations"
            >
              <Search size={18} />
            </button>
            <button
              className={`sidebar-icon-button${showRequestsView ? ' active' : ''}`}
              onClick={() => {
                // /inbox is the view's deep-linkable URL; the route->state
                // sync in App.tsx flips showRequestsView. Set it here too so
                // the view opens without waiting for the navigation effect.
                setShowRequestsView(true);
                navigate('/inbox');
              }}
              title="Requests"
              aria-label="Open requests"
            >
              <Inbox size={18} />
              <RequestsBadge />
            </button>
          </div>
        </div>

        <div className={`sidebar-panels ${drilledProjectId ? 'drilled' : ''} slide-${slideDirection}`}>
          {/* Main panel: projects + conversations */}
          <div className="sidebar-panel sidebar-panel-main">
            {projectsLoaded && (
              <ProjectsSection
                projects={projects}
                showArchived={showArchivedProjects}
                onShowArchivedChange={setShowArchivedProjects}
                onOpenProject={handleProjectDrillDown}
                onCreateProject={() => setShowNewProjectModal(true)}
              />
            )}

            {docsEnabled && (
              <DocsSection
                docs={userDocs.docs}
                loading={userDocs.loading}
                error={userDocs.error}
                activeDocId={activeDocId}
                onOpenDoc={handleOpenDoc}
                onOpenAll={() => handleOpenAllDocs(null)}
                headerActive={docsListProjectId === null}
              />
            )}

            <ConversationsSection
              list={topLevel}
              activeConversationId={highlightedConversationId}
              creating={creating}
              onSelect={(id) => handleConversationClick(id, null)}
              onNewChat={handleNewChat}
              onConvertToProject={(conversation) => setConvertConversation({
                id: conversation.id,
                title: conversation.custom_name || conversation.title,
              })}
              onDuplicateWorkspace={(id) => void handleDuplicateWorkspace(id)}
              onArchived={handleArchived}
            />
          </div>

          {/* Drill-down panel: project conversations */}
          <div className="sidebar-panel sidebar-panel-project">
            {drilledProject && (
              <ProjectPanel
                project={drilledProject}
                conversations={projectConversations.byProject[drilledProject.id] || []}
                loading={Boolean(projectConversations.loadingByProject[drilledProject.id])}
                routines={projectRoutines.byProject[drilledProject.id] || []}
                showRoutines={!drilledProject.public || publicRoutinesEnabled}
                showArchived={showArchivedProject}
                onShowArchivedChange={setShowArchivedProject}
                showSlack={showSlackProject}
                onShowSlackChange={setShowSlackProject}
                expandedRoutineGroups={expandedRoutineGroups}
                onToggleRoutineGroup={toggleRoutineGroup}
                activeConversationId={highlightedConversationId}
                busy={creatingInProject === drilledProject.id}
                onBack={handleDrillDownBack}
                onOpenProjectSettings={() => setSettingsProjectId(drilledProject.id)}
                onNewRoutine={() => setShowNewRoutineModal(true)}
                onRoutineSettings={setSettingsRoutine}
                onRunRoutine={(routine) => handleRunRoutine(drilledProject.id, routine)}
                onNewChat={() => handleNewProjectChat(drilledProject.id)}
                onSelect={(id) => handleConversationClick(id, drilledProject.id)}
                updateConversations={(updater) => projectConversations.update(drilledProject.id, updater)}
                onArchived={handleArchived}
                showDocs={docsEnabled}
                docs={projectDocs.docs}
                docsLoading={projectDocs.loading}
                docsError={projectDocs.error}
                activeDocId={activeDocId}
                onOpenDoc={handleOpenDoc}
                onOpenAllDocs={() => handleOpenAllDocs(drilledProject.id)}
                docsHeaderActive={docsListProjectId === drilledProject.id}
              />
            )}
          </div>
        </div>
      </div>

      {updateAvailable && (
        <div className="version-update-banner">
          Quest has updated. Please <a href="#" onClick={(e) => { e.preventDefault(); window.location.reload(); }}>reload</a> ASAP!
        </div>
      )}

      <UserInfoBar
        email={userEmail}
        name={userName}
        onSettingsClick={() => setSettingsOpen(true)}
      />

      <NewProjectModal
        isOpen={showNewProjectModal}
        onClose={() => setShowNewProjectModal(false)}
        onProjectCreated={handleProjectCreated}
      />

      <ConvertToProjectModal
        isOpen={convertConversation !== null}
        conversationId={convertConversation?.id ?? ''}
        conversationTitle={convertConversation?.title ?? ''}
        onClose={() => setConvertConversation(null)}
        onConverted={handleConvertedToProject}
      />

      <ProjectSettingsModal
        isOpen={settingsProjectId !== null}
        projectId={settingsProjectId}
        onClose={() => setSettingsProjectId(null)}
        onProjectUpdated={handleProjectUpdated}
        onProjectDeleted={handleProjectDeleted}
      />

      <NewRoutineModal
        isOpen={showNewRoutineModal}
        projectId={drilledProjectId}
        isPublicProject={drilledProjectIsPublic}
        onClose={() => setShowNewRoutineModal(false)}
        onRoutineCreated={handleRoutinesChanged}
      />

      <RoutineSettingsModal
        isOpen={settingsRoutine !== null}
        projectId={drilledProjectId}
        isPublicProject={drilledProjectIsPublic}
        routine={settingsRoutine}
        onClose={() => setSettingsRoutine(null)}
        onRoutineUpdated={handleRoutinesChanged}
        onRoutineDeleted={handleRoutinesChanged}
        onOpenRunConversation={(conversationId) => {
          setSettingsRoutine(null);
          onConversationSelect(conversationId, drilledProjectId);
        }}
      />

      <SearchModal
        isOpen={showSearchModal}
        onClose={() => setShowSearchModal(false)}
        onNavigate={(conversationId, projectId, messageIndex) => {
          setShowSearchModal(false);
          setShowRequestsView(false);
          onConversationSelect(conversationId, projectId);
          setScrollToMessageIndex(messageIndex);
        }}
      />
    </div>
  );
});
