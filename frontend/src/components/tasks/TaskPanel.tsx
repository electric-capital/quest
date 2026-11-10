import type { TaskView } from '../../hooks/useTask'

/**
 * PLACEHOLDER brief and instructions panel of the task page, replaced by B4
 * (instructions editor, rendered brief with edit and history, Task files
 * card from B5). For now it shows the raw instructions and brief text and
 * the brief version. Takes the whole TaskView so B4 can merge PATCH / PUT
 * responses with applyDetail / applyBrief. Styles in TaskPage.css.
 */
export function TaskPanel({ task: view }: { task: TaskView }) {
  const task = view.task
  if (!task) return null

  return (
    <div className="task-panel">
      <section className="task-panel-section" aria-labelledby="task-panel-instructions">
        <h2 id="task-panel-instructions" className="task-panel-heading">
          Instructions
        </h2>
        {task.instructions ? (
          <p className="task-panel-text">{task.instructions}</p>
        ) : (
          <p className="task-panel-empty">No instructions</p>
        )}
      </section>

      <section className="task-panel-section" aria-labelledby="task-panel-brief">
        <h2 id="task-panel-brief" className="task-panel-heading">
          Brief
          {task.brief_version > 0 && (
            <span className="task-panel-heading-meta">version {task.brief_version}</span>
          )}
        </h2>
        {task.brief ? (
          <p className="task-panel-text">{task.brief}</p>
        ) : (
          <p className="task-panel-empty">No brief yet</p>
        )}
      </section>
    </div>
  )
}
