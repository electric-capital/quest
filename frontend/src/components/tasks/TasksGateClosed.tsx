import { ListChecks } from 'lucide-react'
import './TasksGateClosed.css'

/**
 * Shown on a /tasks URL while the `tasks` feature gate is closed for the
 * current user (enabled_features on GET /me). Nothing is fetched: every
 * /app/api/tasks route would 403 `tasks_disabled` anyway.
 */
export function TasksGateClosed() {
  return (
    <div className="tasks-gate-closed" role="status">
      <ListChecks size={32} className="tasks-gate-closed-icon" aria-hidden="true" />
      <p className="tasks-gate-closed-title">Quest Tasks is turned off for your account.</p>
      <p className="tasks-gate-closed-hint">Ask an admin to enable it in Settings &gt; Features.</p>
    </div>
  )
}
