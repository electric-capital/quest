import { useCallback, useEffect, useId, useMemo, useState } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import './AutoProjectPage.css'
import { autoprojectSource, type FileSource } from '../../api/fileApi'
import { useIsMobile } from '../../hooks/useIsMobile'
import { useAutoProject } from '../../hooks/useAutoProject'
import { useAutoProjectRuns } from '../../hooks/useAutoProjectRuns'
import { useMarkAutoProjectViewed } from '../../hooks/useAutoProjectUnread'
import { overwritePromptText, useWorkspaceCopy } from '../../hooks/useWorkspaceCopy'
import { useWorkspaceDownload } from '../../hooks/useWorkspaceDownload'
import { autoprojectRunPath, autoprojectThreadPath } from '../../utils/autoprojectsRoute'
import { DocConfirmDialog } from '../docs/DocConfirmDialog'
import { FileBrowser, isCsvFile, isImageFile, isJsonFile, isPdfFile, isViewableFile, type FileBrowserListing } from '../FileBrowser'
import { LazyFileViewerModal } from '../LazyFileViewerModal'
import { AutoProjectBrief } from './AutoProjectBrief'
import { AutoProjectBriefHistory } from './AutoProjectBriefHistory'
import { AutoProjectDetailPanel } from './AutoProjectDetailPanel'
import { AutoProjectMainColumn } from './AutoProjectMainColumn'
import { AutoProjectNotice, readAutoProjectNotice } from './AutoProjectNotice'
import { AutoProjectRunPanel } from './AutoProjectRunPanel'
import { AutoProjectLoadError, AutoProjectNotFound, AutoProjectViewLoading } from './AutoProjectViewState'

/**
 * Router state on a run URL pushed by this page ("View run"): closing the
 * panel then goes back in history instead of pushing the AutoProject URL, so
 * Back and the close button agree.
 */
const RUN_PANEL_FROM_PAGE = 'autoprojectRunPanelFromPage'

function openedFromPage(state: unknown): boolean {
  return typeof state === 'object' && state !== null && (state as Record<string, unknown>)[RUN_PANEL_FROM_PAGE] === true
}

/** A workspace file open in the page's viewer: of some run, or of the AutoProject. */
interface ViewerFile {
  source: FileSource
  path: string
  filename: string
}

/**
 * The AutoProject page (/autoprojects/<id>, and /autoprojects/<id>/runs/<conversation id> with
 * that run open in the detail panel; AutoProjectsMain keys it by AutoProject id, so
 * moving between the two keeps the page).
 *
 * The main column (AutoProjectMainColumn) holds, top to bottom: the header, then
 * three cards: the Brief, the page's main content (AutoProjectBrief, edited in
 * place), AutoProject files (a compact FileBrowser over the AutoProject workspace whose
 * toolbar is the card's header row) and Updates (one per run, in the
 * viewer's sort order, AutoProjectUpdates), with the composer in the page flow
 * between the AutoProject files and Updates cards. The page opens at the top. Without a
 * detail view the column takes the whole width at the chat's reading
 * measure, centred.
 *
 * The detail panel (AutoProjectDetailPanel) is closed by default and shows one of
 * two views at a time, beside the main column on desktop and as a
 * full-screen view with "Back to AutoProject" on phones:
 *
 * - a run's transcript (AutoProjectRunPanel): "View run" on an update. The run is
 *   in the URL (`runConversationId`), so run links, the inbox, the
 *   `/chats/<run id>` redirect, reload and Back all work; "View run" pushes
 *   the run URL (switching from another run replaces it), and closing goes
 *   back when this page pushed it, else replaces it with the AutoProject URL. A
 *   run that is not this AutoProject's shows the panel's not-found state.
 * - the brief History (AutoProjectBriefHistory): the brief's History button,
 *   which toggles it; plain component state, no URL.
 *
 * Copy / Move between a run's Chat Files (in the run panel) and the AutoProject
 * files card (in the main column) come from one useWorkspaceCopy over the
 * run open in the panel, once it resolved; with no run open the AutoProject files
 * card has no copy actions.
 *
 * Read state: the AutoProject counts as seen, and the unread store acknowledges
 * it (useMarkAutoProjectViewed), only while its newest update is actually on
 * screen (AutoProjectMainColumn's `onNewestVisibleChange`; not while the phone's
 * full-screen panel covers it) or the panel shows the newest run; an AutoProject
 * without runs counts as seen. Opening at the top therefore never marks a
 * new update read that was never shown.
 *
 * One file viewer serves the brief's and the results' workspace links
 * (`autoproject://` and run files); the AutoProject files card keeps its own.
 *
 * A notice carried in the router state (AutoProjectPageLocationState, e.g. the
 * New AutoProject page's "created, but the run did not start") shows under the
 * header until dismissed; dismissing replaces the history entry's state so
 * a reload does not bring it back.
 */
export function AutoProjectPage({ autoprojectId, runConversationId }: { autoprojectId: string; runConversationId: string | null }) {
  const view = useAutoProject(autoprojectId)
  const runs = useAutoProjectRuns(autoprojectId)
  const { autoproject, loading, error, notFound, refresh } = view
  const isMobile = useIsMobile()
  const filesHeadingId = useId()

  const location = useLocation()
  const navigate = useNavigate()
  const notice = readAutoProjectNotice(location.state)
  const dismissNotice = useCallback(() => {
    navigate(`${location.pathname}${location.search}`, { replace: true, state: null })
  }, [navigate, location.pathname, location.search])

  // --- Detail panel ------------------------------------------------------------

  const [historyOpen, setHistoryOpen] = useState(false)
  // A run in the URL takes the panel.
  useEffect(() => {
    if (runConversationId) setHistoryOpen(false)
  }, [runConversationId])

  const fromPage = openedFromPage(location.state)
  const routeState = location.state
  const closeRunRoute = useCallback(() => {
    if (fromPage) navigate(-1)
    else navigate(autoprojectThreadPath(autoprojectId), { replace: true })
  }, [fromPage, navigate, autoprojectId])

  const closePanel = useCallback(() => {
    if (runConversationId) closeRunRoute()
    else setHistoryOpen(false)
  }, [runConversationId, closeRunRoute])

  const viewRun = useCallback(
    (conversationId: string) => {
      if (runConversationId === conversationId) {
        closeRunRoute()
        return
      }
      setHistoryOpen(false)
      if (runConversationId) {
        navigate(autoprojectRunPath(autoprojectId, conversationId), { replace: true, state: routeState })
      } else {
        navigate(autoprojectRunPath(autoprojectId, conversationId), { state: { [RUN_PANEL_FROM_PAGE]: true } })
      }
    },
    [runConversationId, closeRunRoute, navigate, autoprojectId, routeState],
  )

  // History toggles; over a run it replaces the run.
  const openHistory = useCallback(() => {
    if (runConversationId) {
      closeRunRoute()
      setHistoryOpen(true)
    } else {
      setHistoryOpen((open) => !open)
    }
  }, [runConversationId, closeRunRoute])

  // --- Run panel resolution and the files copy -----------------------------------

  // The run panel's conversation and its seq once it resolved as a run of this AutoProject.
  const [resolvedRun, setResolvedRun] = useState<{ conversationId: string; seq: number } | null>(null)
  const handleRunResolved = useCallback((conversationId: string, seq: number | null) => {
    setResolvedRun((prev) =>
      seq !== null
        ? prev?.conversationId === conversationId && prev.seq === seq
          ? prev
          : { conversationId, seq }
        : prev?.conversationId === conversationId
          ? null
          : prev,
    )
  }, [])
  const panelRunSeq =
    runConversationId !== null && resolvedRun?.conversationId === runConversationId ? resolvedRun.seq : null
  const panelRun = panelRunSeq !== null ? runConversationId : null

  const filesSource = useMemo(() => autoprojectSource(autoprojectId), [autoprojectId])
  const copy = useWorkspaceCopy(panelRun, filesSource)
  const prompt = copy.overwritePrompt
  const promptText = prompt ? overwritePromptText(prompt) : null

  // Entries at the AutoProject workspace root, from the card's own listing; null
  // until it listed the root (it may restore a subfolder it was left in).
  const [rootEntryCount, setRootEntryCount] = useState<number | null>(null)
  const handleFilesListing = useCallback((listing: FileBrowserListing) => {
    if (listing.path === '/') setRootEntryCount(listing.count)
  }, [])

  // --- Read state ------------------------------------------------------------------

  const [newestVisible, setNewestVisible] = useState(false)
  const runList = runs.runs
  const newestRun = runList.length > 0 ? runList[runList.length - 1] : null
  const panelCoversColumn = isMobile && (runConversationId !== null || historyOpen)
  const newestSeen =
    autoproject !== null &&
    !runs.loading &&
    (newestRun === null
      ? runs.error === null
      : (newestVisible && !panelCoversColumn) || panelRun === newestRun.conversation_id)
  useMarkAutoProjectViewed(autoprojectId, newestSeen)

  // --- File viewer for the brief's and the results' links --------------------------

  const downloadWorkspaceFile = useWorkspaceDownload()
  const [viewer, setViewer] = useState<ViewerFile | null>(null)
  const handleOpenFile = useCallback(
    (path: string, filename: string, source: FileSource) => {
      if (isViewableFile(filename)) {
        setViewer({ source, path, filename })
      } else {
        void downloadWorkspaceFile(source, path).catch(() => {
          // Swallow; the link stays clickable and the user can retry.
        })
      }
    },
    [downloadWorkspaceFile],
  )
  const handleDownloadViewerFile = useCallback(async () => {
    if (!viewer) return
    try {
      await downloadWorkspaceFile(viewer.source, viewer.path)
    } catch {
      // Swallow; the modal stays open and the user can retry.
    }
  }, [downloadWorkspaceFile, viewer])

  // --- Render ------------------------------------------------------------------------

  if (loading) {
    return <AutoProjectViewLoading text="Loading AutoProject..." />
  }

  if (notFound) {
    return <AutoProjectNotFound />
  }

  if (!autoproject) {
    return <AutoProjectLoadError message={error?.message || 'Failed to load the AutoProject.'} onRetry={refresh} />
  }

  const lead = (
    <>
      <AutoProjectBrief
        autoproject={autoproject}
        onBrief={view.applyBrief}
        onStale={() => void refresh()}
        historyOpen={!runConversationId && historyOpen}
        onOpenHistory={openHistory}
        onOpenFile={handleOpenFile}
      />
      <section className="autoproject-card autoproject-files" aria-labelledby={filesHeadingId}>
        <FileBrowser
          source={filesSource}
          hideTitle
          compact
          toolbarLead={
            <>
              <h2 id={filesHeadingId} className="autoproject-card-title">
                AutoProject files
              </h2>
              <span className="autoproject-card-meta">{autoprojectFilesSummary(rootEntryCount)}</span>
            </>
          }
          onListing={handleFilesListing}
          rowActions={copy.sharedRowActions}
          notice={copy.notices.autoproject}
          onDismissNotice={() => copy.dismissNotice('autoproject')}
        />
      </section>
    </>
  )

  let panel = null
  if (runConversationId) {
    panel = (
      <AutoProjectRunPanel
        key={runConversationId}
        autoproject={autoproject}
        changeSeq={view.changeSeq}
        conversationId={runConversationId}
        copy={copy}
        onResolved={handleRunResolved}
        onClose={closePanel}
        onOpenFile={handleOpenFile}
      />
    )
  } else if (historyOpen) {
    panel = (
      <AutoProjectDetailPanel key="history" title="Brief history" onClose={closePanel}>
        <AutoProjectBriefHistory autoproject={autoproject} onRestored={view.applyBrief} onStale={() => void refresh()} />
      </AutoProjectDetailPanel>
    )
  }

  return (
    <div className={`autoproject-page${panel ? ' has-panel' : ''}`}>
      <AutoProjectMainColumn
        autoprojectId={autoprojectId}
        autoproject={autoproject}
        runs={runs}
        onAutoProjectDetail={view.applyDetail}
        notice={
          notice ? (
            <div className="autoproject-page-notice">
              <AutoProjectNotice message={notice} onDismiss={dismissNotice} />
            </div>
          ) : null
        }
        lead={lead}
        openRunConversationId={runConversationId}
        openRunSeq={panelRunSeq}
        onViewRun={viewRun}
        onOpenFile={handleOpenFile}
        onNewestVisibleChange={setNewestVisible}
      />
      {panel}

      <DocConfirmDialog
        isOpen={prompt !== null}
        title={promptText?.title ?? ''}
        confirmLabel={promptText?.confirmLabel ?? 'Replace'}
        busyLabel="Replacing..."
        tone="danger"
        busy={copy.overwriteBusy}
        error={copy.overwriteError}
        onConfirm={copy.confirmOverwrite}
        onClose={copy.cancelOverwrite}
      >
        {promptText && <p>{promptText.body}</p>}
      </DocConfirmDialog>

      {viewer && (
        <LazyFileViewerModal
          isOpen
          source={viewer.source}
          filePath={viewer.path}
          fileName={viewer.filename}
          isImage={isImageFile(viewer.filename)}
          isJson={isJsonFile(viewer.filename)}
          isPdf={isPdfFile(viewer.filename)}
          isCsv={isCsvFile(viewer.filename)}
          onClose={() => setViewer(null)}
          onDownload={handleDownloadViewerFile}
        />
      )}
    </div>
  )
}

/** The AutoProject files card's count: what the workspace root holds (nothing while empty or unknown). */
function autoprojectFilesSummary(rootEntryCount: number | null): string {
  if (rootEntryCount === null || rootEntryCount === 0) return ''
  return `${rootEntryCount} item${rootEntryCount === 1 ? '' : 's'}`
}
