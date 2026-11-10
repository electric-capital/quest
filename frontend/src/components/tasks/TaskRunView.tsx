import { Link } from 'react-router-dom'
import { ChevronLeft } from 'lucide-react'
import './TaskRunView.css'
import { useTask } from '../../hooks/useTask'
import { taskThreadPath } from '../../utils/tasksRoute'
import { TaskLoadError, TaskViewState } from './TaskViewState'

/**
 * The run view (/tasks/<id>/runs/<conversation id>): one run's transcript
 * under a "Back to task" header naming the task. The header and its task
 * lookup are the lasting frame; the body is a PLACEHOLDER that B5 replaces
 * with ChatPanel for the run conversation (read-only "task" origin) and its
 * Chat Files card. The run conversation is never the active sidebar
 * conversation (see the route note in App.tsx).
 */
export function TaskRunView({ taskId, conversationId }: { taskId: string; conversationId: string }) {
  const { task, loading, error, notFound, refresh } = useTask(taskId)

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
    <div className="task-run-view">
      <header className="task-run-view-header">
        <Link to={taskThreadPath(taskId)} className="task-run-view-back">
          <ChevronLeft size={16} aria-hidden="true" />
          Back to task
        </Link>
        <h1 className="task-run-view-title">{task.title}</h1>
      </header>
      <p className="task-run-view-placeholder">
        The run transcript view arrives in B5 (conversation {conversationId}).
      </p>
    </div>
  )
}
