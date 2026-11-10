import { useCallback } from 'react'
import { Link, useLocation, useNavigate } from 'react-router-dom'
import './TaskPage.css'
import { useTask } from '../../hooks/useTask'
import { useTaskRuns } from '../../hooks/useTaskRuns'
import { useMarkTaskViewed } from '../../hooks/useTaskUnread'
import { TaskHeader } from './TaskHeader'
import { TaskThread } from './TaskThread'
import { TaskPanel } from './TaskPanel'
import { TaskLoadError, TaskViewState } from './TaskViewState'
import { TaskNotice, readTaskNotice } from './TaskNotice'

/**
 * The task page (/tasks/<id>): the header over a two-column body, the
 * thread (TaskThread) on the left and the brief and instructions panel
 * (TaskPanel) on the right, in the place the RightPanel takes beside a chat
 * (spec 8.3). Below 768px the panel stacks under the thread.
 *
 * This shell is the lasting page frame: it owns the one useTask and the one
 * useTaskRuns of the page and passes them down, and acknowledges an unread
 * task while it is on screen in a focused window. The bodies of the header
 * (B4 adds the rename / archive / delete menu), the thread (B3) and the
 * panel (B4) are replaced in later phases. App.tsx keys it by task id, so
 * switching tasks starts from a fresh state.
 *
 * A notice carried in the router state (TaskPageLocationState, e.g. the
 * New Task page's "created, but the run did not start") shows above the
 * body until dismissed; dismissing replaces the history entry's state so a
 * reload does not bring it back.
 */
export function TaskPage({ taskId }: { taskId: string }) {
  const view = useTask(taskId)
  const runs = useTaskRuns(taskId)
  const { task, loading, error, notFound, refresh } = view

  useMarkTaskViewed(taskId, task !== null)

  const location = useLocation()
  const navigate = useNavigate()
  const notice = readTaskNotice(location.state)
  const dismissNotice = useCallback(() => {
    navigate(`${location.pathname}${location.search}`, { replace: true, state: null })
  }, [navigate, location.pathname, location.search])

  if (loading) {
    return (
      <TaskViewState>
        <p className="task-view-state-text">Loading task...</p>
      </TaskViewState>
    )
  }

  if (notFound) {
    return (
      <TaskViewState>
        <p className="task-view-state-text">This task does not exist or was deleted.</p>
        <Link to="/" className="task-view-state-link">
          Back to home
        </Link>
      </TaskViewState>
    )
  }

  if (!task) {
    return <TaskLoadError message={error?.message || 'Failed to load the task.'} onRetry={refresh} />
  }

  return (
    <div className="task-page">
      <TaskHeader task={task} />
      {notice && (
        <div className="task-page-notice">
          <TaskNotice message={notice} onDismiss={dismissNotice} />
        </div>
      )}
      <div className="task-page-body">
        <div className="task-page-thread">
          <TaskThread taskId={taskId} task={task} runs={runs} />
        </div>
        <aside className="task-page-panel" aria-label="Task brief and instructions">
          <TaskPanel task={view} />
        </aside>
      </div>
    </div>
  )
}
