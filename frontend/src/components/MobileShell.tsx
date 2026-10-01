import { useState, useEffect, useCallback, useRef } from 'react'
import { Menu, Folder, SquarePen } from 'lucide-react'
import { useNavigate } from 'react-router-dom'
import './MobileShell.css'
import { useAppConfig } from '../contexts/AppConfigContext'
import { useNavigationState } from '../contexts/NavigationContext'
import { useProjects } from '../contexts/ProjectsContext'
import { Sidebar } from './Sidebar'
import { ChatPanel } from './ChatPanel'
import { HomeComposer } from './HomeComposer'
import { RequestsView } from './RequestsView'
import { RightPanel } from './RightPanel'
import { SettingsModal } from './SettingsModal'

interface MobileShellProps {
  activeConversationId: string | null
  projectId: string | null
  onConversationSelect: (id: string, projectId?: string | null) => void
  onNewConversation: (id: string, projectId?: string | null) => void
  onProjectIdLoaded: (conversationId: string, projectId: string) => void
}

interface ConversationSelectOpts {
  implicit?: boolean
}

/**
 * Phone-width shell rendered by AppContent instead of the desktop
 * Sidebar/ChatPanel/RightPanel row when useIsMobile() matches. Same routes,
 * same data layer — only the layout tree differs: a fixed top bar, a slide-in
 * drawer hosting the existing Sidebar, and a single main pane (chat, home
 * composer, or requests). The desktop-only RightPanel is intentionally absent.
 */
export function MobileShell({
  activeConversationId,
  projectId,
  onConversationSelect,
  onNewConversation,
  onProjectIdLoaded,
}: MobileShellProps) {
  const { appName } = useAppConfig()
  const {
    isSettingsOpen,
    setSettingsOpen,
    showRequestsView,
    setShowRequestsView,
  } = useNavigationState()
  const { drilledProjectId, projects } = useProjects()
  const navigate = useNavigate()

  // While the sidebar is drilled into a project, the top bar names that
  // project (folder glyph + name) instead of the app.
  const drilledProject = drilledProjectId
    ? projects.find((p) => p.id === drilledProjectId) ?? null
    : null

  const [drawerOpen, setDrawerOpen] = useState(false)
  const [workspaceOpen, setWorkspaceOpen] = useState(false)
  const shellRef = useRef<HTMLDivElement>(null)

  // iOS Safari viewport hardening. Two failure modes without this:
  // 1. The document itself can be panned/rubber-banded, leaving the shell in
  //    a stuck state (top bar off-screen, gap under the composer). Lock
  //    document scrolling while the shell is mounted.
  // 2. The on-screen keyboard shrinks only the *visual* viewport; Safari then
  //    pans the page to reveal the focused composer. Instead, size the shell
  //    to the visual viewport so the layout resizes and nothing pans, and
  //    reset any residual pan when the keyboard opens/closes.
  useEffect(() => {
    document.documentElement.classList.add('mobile-shell-lock')
    document.body.classList.add('mobile-shell-lock')

    const vv = window.visualViewport
    const applyVisualViewport = () => {
      const shell = shellRef.current
      if (!shell || !vv) return
      // Cover exactly the visible area: height from the visual viewport, and
      // translate by its pan offset. Safari pans the visual viewport for the
      // keyboard even when the document can't scroll, and that pan can't be
      // programmatically reset — so the shell follows it instead, keeping the
      // top bar on-screen and the composer glued above the keyboard.
      shell.style.height = `${vv.height}px`
      shell.style.transform = vv.offsetTop > 0 ? `translateY(${vv.offsetTop}px)` : ''
    }
    vv?.addEventListener('resize', applyVisualViewport)
    vv?.addEventListener('scroll', applyVisualViewport)
    applyVisualViewport()

    return () => {
      vv?.removeEventListener('resize', applyVisualViewport)
      vv?.removeEventListener('scroll', applyVisualViewport)
      if (shellRef.current) {
        shellRef.current.style.height = ''
        shellRef.current.style.transform = ''
      }
      document.documentElement.classList.remove('mobile-shell-lock')
      document.body.classList.remove('mobile-shell-lock')
    }
  }, [])

  // Close both overlays when the Requests view or Settings is toggled (taps
  // inside the drawer's Sidebar). Conversation changes are NOT a close
  // trigger here: explicit picks close the drawer in the wrapped handlers
  // below, while the selections the Sidebar emits as a side effect of
  // drilling in/out of a project (marked opts.implicit) must keep the drawer
  // open — the user is still navigating the drawer, not leaving it.
  useEffect(() => {
    setDrawerOpen(false)
    setWorkspaceOpen(false)
  }, [showRequestsView, isSettingsOpen])

  const closeOverlays = useCallback(() => {
    setDrawerOpen(false)
    setWorkspaceOpen(false)
  }, [])

  const handleConversationSelect = useCallback(
    (id: string, projectId?: string | null, opts?: ConversationSelectOpts) => {
      if (!opts?.implicit) {
        setDrawerOpen(false)
      }
      onConversationSelect(id, projectId)
    },
    [onConversationSelect],
  )

  const handleNewConversation = useCallback(
    (id: string, projectId?: string | null) => {
      setDrawerOpen(false)
      onNewConversation(id, projectId)
    },
    [onNewConversation],
  )

  // Top-bar "new chat": go to the home composer, which creates the
  // conversation on first send (inside the drilled project, if any -- see
  // HomeComposer). Nothing is created until the user actually sends.
  const handleTopbarNewChat = useCallback(() => {
    setDrawerOpen(false)
    setWorkspaceOpen(false)
    setShowRequestsView(false)
    navigate('/')
  }, [navigate, setShowRequestsView])

  return (
    <div className="mobile-shell" ref={shellRef}>
      <header className="mobile-topbar">
        <button
          className="mobile-topbar-button"
          onClick={() => {
            setWorkspaceOpen(false)
            setDrawerOpen(true)
          }}
          aria-label="Open menu"
        >
          <Menu size={22} />
        </button>
        <button
          className="mobile-topbar-button"
          onClick={handleTopbarNewChat}
          aria-label="New chat"
        >
          <SquarePen size={20} />
        </button>
        <div className="mobile-topbar-title">
          {drilledProject ? (
            <>
              <Folder size={16} className="mobile-topbar-title-icon" aria-hidden="true" />
              <span className="mobile-topbar-title-text">{drilledProject.name}</span>
            </>
          ) : (
            <span className="mobile-topbar-title-text">{appName}</span>
          )}
        </div>
        <button
          className="mobile-topbar-button mobile-topbar-button-end"
          onClick={() => {
            setDrawerOpen(false)
            setWorkspaceOpen(true)
          }}
          aria-label="Open workspace"
        >
          <Folder size={20} />
        </button>
      </header>

      <div className="mobile-main">
        {showRequestsView ? (
          <RequestsView />
        ) : activeConversationId ? (
          <ChatPanel
            conversationId={activeConversationId}
            onProjectIdLoaded={onProjectIdLoaded}
          />
        ) : (
          <HomeComposer onNewConversation={handleNewConversation} />
        )}
      </div>

      <div
        className={`mobile-drawer-overlay${drawerOpen || workspaceOpen ? ' open' : ''}`}
        onClick={closeOverlays}
      />
      <div className={`mobile-drawer${drawerOpen ? ' open' : ''}`}>
        <Sidebar
          activeConversationId={activeConversationId}
          onConversationSelect={handleConversationSelect}
          onNewConversation={handleNewConversation}
        />
      </div>
      <div className={`mobile-workspace-drawer${workspaceOpen ? ' open' : ''}`}>
        <RightPanel
          conversationId={activeConversationId}
          projectId={projectId}
        />
      </div>

      <SettingsModal isOpen={isSettingsOpen} onClose={() => setSettingsOpen(false)} />
    </div>
  )
}
