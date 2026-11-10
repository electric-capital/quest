import type { Task } from '../../api/types'

/**
 * The task page header: the task title as the page heading, and a quiet
 * "Archived" chip on an archived task. B4 adds the header menu (rename,
 * archive / unarchive, delete) beside the title. Styles in TaskPage.css.
 */
export function TaskHeader({ task }: { task: Task }) {
  return (
    <header className="task-header">
      <h1 className="task-header-title">{task.title}</h1>
      {task.archived && <span className="task-header-chip">Archived</span>}
    </header>
  )
}
