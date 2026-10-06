import { useEffect, useCallback } from 'react'
import { Routes, Route, Navigate, useParams, useNavigate, useLocation } from 'react-router-dom'
import './App.css'
import { AppProviders } from './contexts/AppProviders'
import { useAppConfig } from './contexts/AppConfigContext'
import { useAuth } from './contexts/AuthContext'
import { useNavigationState } from './contexts/NavigationContext'
import { useProjects } from './contexts/ProjectsContext'
import { Sidebar } from './components/Sidebar'
import { ChatPanel } from './components/ChatPanel'
import { HomeComposer } from './components/HomeComposer'
import { RightPanel } from './components/RightPanel'
import { SettingsModal } from './components/SettingsModal'
import { SignInScreen } from './components/SignInScreen'
import { SetPasswordScreen } from './components/SetPasswordScreen'
import { RequestsView } from './components/RequestsView'
import { AdminSystemReportsPage } from './pages/AdminSystemReportsPage'
import { MobileShell } from './components/MobileShell'
import { DocsListView } from './components/docs/DocsListView'
import { DocViewer } from './components/docs/DocViewer'
import { DocsGateClosed } from './components/docs/DocsGateClosed'
import { DOCS_FEATURE } from './api/docsApi'
import { useIsMobile } from './hooks/useIsMobile'
import { parseDocsRoute } from './utils/docsRoute'

function AppContent() {
  const {
    activeConversationId,
    setActiveConversationId,
    isSettingsOpen,
    setSettingsOpen,
    showRequestsView,
    setShowRequestsView,
  } = useNavigationState()
  const { isAuthenticated, isCheckingAuth, hasAnyServiceConnected, enabledFeatures } = useAuth()
  const { appName } = useAppConfig()
  const { setActiveProjectId } = useProjects()

  const params = useParams<{ conversationId?: string; projectId?: string }>()
  const navigate = useNavigate()
  const location = useLocation()
  const isMobile = useIsMobile()

  // Quest Docs views (/docs, /docs?project=<id>, /docs/<id>) take over the
  // main pane like the requests inbox. The URL is the only state: no
  // NavigationContext flag, and activeConversationId is null on these routes.
  const docsRoute = parseDocsRoute(location.pathname, location.search)
  const docsEnabled = enabledFeatures.includes(DOCS_FEATURE)

  // Sync URL -> requests-inbox state. /inbox is the deep-linkable address of
  // the RequestsView (linked from Slack pending-request reminder DMs); any
  // navigation away (conversation click, new chat, browser back) closes it.
  const isInboxRoute = location.pathname === '/inbox'
  useEffect(() => {
    setShowRequestsView(isInboxRoute)
  }, [isInboxRoute, setShowRequestsView])

  // Sync URL params -> context state
  // The URL is the source of truth; this effect mirrors URL params into context
  // so that components reading from context (Sidebar, etc.) stay in sync.
  useEffect(() => {
    const urlConvoId = params.conversationId ?? null
    const urlProjectId = params.projectId ?? null
    setActiveConversationId(urlConvoId)
    setActiveProjectId(urlProjectId)
  }, [params.conversationId, params.projectId, setActiveConversationId, setActiveProjectId])

  // Set the browser tab title (overrides the static <title> from index.html)
  useEffect(() => {
    document.title = appName;
  }, [appName]);

  // Auto-open settings for new users who haven't connected any services.
  // Desktop only: on mobile the full-screen settings takeover would hijack
  // the first view, so new users find Data Connections on their own.
  useEffect(() => {
    if (isAuthenticated && !hasAnyServiceConnected && !isMobile) {
      setSettingsOpen(true);
    }
  }, [isAuthenticated, hasAnyServiceConnected, isMobile, setSettingsOpen]);

  // Handle conversation selection (with optional project context)
  // Now navigates to the appropriate URL instead of setting state directly
  const handleConversationSelect = useCallback((id: string, projectId?: string | null) => {
    if (projectId && id) {
      navigate(`/projects/${projectId}/${id}`)
    } else if (id) {
      navigate(`/chats/${id}`)
    } else {
      navigate('/')
    }
  }, [navigate])

  // Handle new conversation creation (with optional project context)
  // Now navigates to the appropriate URL instead of setting state directly
  const handleNewConversation = useCallback((id: string, projectId?: string | null) => {
    if (projectId && id) {
      navigate(`/projects/${projectId}/${id}`)
    } else if (id) {
      navigate(`/chats/${id}`)
    } else {
      navigate('/')
    }
  }, [navigate])

  // Handle edge case: conversation loaded via /chats/<id> but actually belongs to a project.
  // The ChatPanel will call this when the conversation detail reveals a project_id.
  const handleProjectIdLoaded = useCallback((conversationId: string, projectId: string) => {
    // Only redirect if we're on /chats/<id> (no projectId in URL params)
    if (!params.projectId && params.conversationId === conversationId) {
      navigate(`/projects/${projectId}/${conversationId}`, { replace: true })
    }
  }, [params.projectId, params.conversationId, navigate])

  // Show loading state while checking session auth
  if (isCheckingAuth) {
    return (
      <div className="app-container">
        <div className="api-key-form-container">
          <h1>{appName}</h1>
          <p>Loading...</p>
        </div>
      </div>
    )
  }

  // If not authenticated, show inline sign-in screen
  if (!isAuthenticated) {
    return <SignInScreen />
  }

  // Phone-width viewports get the adaptive mobile shell (same routes and data
  // layer, different layout tree). Switches live when crossing the breakpoint.
  if (isMobile) {
    return (
      <MobileShell
        activeConversationId={activeConversationId}
        projectId={params.projectId ?? null}
        onConversationSelect={handleConversationSelect}
        onNewConversation={handleNewConversation}
        onProjectIdLoaded={handleProjectIdLoaded}
        docsRoute={docsRoute}
        docsEnabled={docsEnabled}
      />
    )
  }

  return (
    <div className="app-container">
      <Sidebar
        activeConversationId={activeConversationId}
        onConversationSelect={handleConversationSelect}
        onNewConversation={handleNewConversation}
      />
      {showRequestsView ? (
        <RequestsView />
      ) : docsRoute ? (
        // Full takeover like RequestsView: no RightPanel beside a docs view.
        <div className="main-content docs-main">
          {!docsEnabled ? (
            <DocsGateClosed />
          ) : docsRoute.kind === 'list' ? (
            <DocsListView projectId={docsRoute.projectId} />
          ) : (
            <DocViewer docId={docsRoute.docId} />
          )}
        </div>
      ) : (
        <>
          <div className="main-content">
            {activeConversationId ? (
              <ChatPanel
                conversationId={activeConversationId}
                onProjectIdLoaded={handleProjectIdLoaded}
              />
            ) : (
              <HomeComposer onNewConversation={handleNewConversation} />
            )}
          </div>
          <RightPanel
            conversationId={activeConversationId}
            projectId={params.projectId ?? null}
          />
        </>
      )}
      <SettingsModal
        isOpen={isSettingsOpen}
        onClose={() => setSettingsOpen(false)}
      />
    </div>
  )
}

function AdminSystemReportsRoute() {
  const { isAuthenticated, isCheckingAuth } = useAuth();
  const { appName } = useAppConfig();

  if (isCheckingAuth) {
    return (
      <div className="app-container">
        <div className="api-key-form-container">
          <h1>{appName}</h1>
          <p>Loading...</p>
        </div>
      </div>
    );
  }

  if (!isAuthenticated) {
    return <SignInScreen />;
  }

  return <AdminSystemReportsPage />;
}

function App() {
  return (
    <AppProviders>
      <Routes>
        <Route path="/" element={<AppContent />} />
        <Route path="/chats/:conversationId" element={<AppContent />} />
        <Route path="/projects/:projectId/:conversationId" element={<AppContent />} />
        <Route path="/inbox" element={<AppContent />} />
        {/* Quest Docs: All Docs (?project=<id> filter) and the viewer. */}
        <Route path="/docs" element={<AppContent />} />
        <Route path="/docs/:docId" element={<AppContent />} />
        {/* Set-password links (invites, sign-up, resets); works signed out. */}
        <Route path="/set-password" element={<SetPasswordScreen />} />
        <Route path="/admin/system-reports" element={<AdminSystemReportsRoute />} />
        {/* Legacy deep links from before the "System Reports" rename. */}
        <Route path="/admin/system-monitor" element={<Navigate to="/admin/system-reports" replace />} />
      </Routes>
    </AppProviders>
  )
}

export default App
