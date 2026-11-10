import './NewTaskPage.css'

/**
 * The New Task page (/tasks/new). PLACEHOLDER replaced by B2 with an empty
 * thread whose composer creates the task on first send (POST /tasks with
 * the title cut from the prompt, then the run; spec 8.1).
 */
export function NewTaskPage() {
  return (
    <div className="task-new-page">
      <h1 className="task-new-page-title">New task</h1>
      <p className="task-new-page-note">The composer that starts a task arrives in B2.</p>
    </div>
  )
}
