import { Link } from 'react-router-dom'
import { ListChecks, Plus } from 'lucide-react'
import './TasksListView.css'
import type { TaskListRow } from '../../api/types'
import { useTasks } from '../../hooks/useTasks'
import { useTaskUnread } from '../../hooks/useTaskUnread'
import { newTaskPath, taskThreadPath, tasksListPath } from '../../utils/tasksRoute'
import { TaskLoadError, TaskViewState } from './TaskViewState'

/**
 * The tasks list (/tasks, /tasks?archived=true): the user's unarchived
 * tasks or the archived ones, by last activity, each row linking to its
 * thread with the active run's working / "needs you" mark and the unread
 * dot. A minimal real list built in B1; B2 owns it afterwards (the archived
 * list it links from the sidebar's Tasks section).
 */
export function TasksListView({ archived }: { archived: boolean }) {
  const { tasks, loading, error, refresh } = useTasks({ archived })

  let body
  if (loading && tasks.length === 0) {
    body = (
      <TaskViewState>
        <p className="task-view-state-text">Loading tasks...</p>
      </TaskViewState>
    )
  } else if (error && tasks.length === 0) {
    body = <TaskLoadError message={error} onRetry={refresh} />
  } else if (tasks.length === 0) {
    body = (
      <p className="tasks-list-empty">{archived ? 'No archived tasks.' : 'No tasks yet.'}</p>
    )
  } else {
    body = (
      <ul className="tasks-list-rows">
        {tasks.map((row) => (
          <TasksListRow key={row.id} row={row} />
        ))}
      </ul>
    )
  }

  return (
    <div className="tasks-list">
      <header className="tasks-list-header">
        <h1 className="tasks-list-title">
          <ListChecks size={20} className="tasks-list-title-icon" aria-hidden="true" />
          {archived ? 'Archived tasks' : 'Tasks'}
        </h1>
        <div className="tasks-list-actions">
          <Link to={tasksListPath({ archived: !archived })} className="tasks-list-switch">
            {archived ? 'Active tasks' : 'Archived tasks'}
          </Link>
          {!archived && (
            <Link to={newTaskPath()} className="tasks-list-new">
              <Plus size={16} aria-hidden="true" />
              New task
            </Link>
          )}
        </div>
      </header>
      <div className="tasks-list-content">{body}</div>
    </div>
  )
}

function TasksListRow({ row }: { row: TaskListRow }) {
  const unread = useTaskUnread(row)
  const active = row.active_run

  return (
    <li>
      <Link to={taskThreadPath(row.id)} className="tasks-list-row">
        <span className={`tasks-list-row-title${unread ? ' unread' : ''}`}>{row.title}</span>
        {active && (
          <span className={`tasks-list-row-state${active.status === 'waiting' ? ' waiting' : ''}`}>
            {active.status === 'waiting' ? 'Needs you' : 'Working'}
          </span>
        )}
        {unread && (
          <span className="tasks-list-row-unread" role="img" aria-label="Unread" />
        )}
      </Link>
    </li>
  )
}
