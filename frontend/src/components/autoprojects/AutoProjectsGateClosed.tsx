import { ListChecks } from 'lucide-react'
import './AutoProjectsGateClosed.css'

/**
 * Shown on a /autoprojects URL while the `autoprojects` feature gate is closed for the
 * current user (enabled_features on GET /me). Nothing is fetched: every
 * /app/api/autoprojects route would 403 `autoprojects_disabled` anyway.
 */
export function AutoProjectsGateClosed() {
  return (
    <div className="autoprojects-gate-closed" role="status">
      <ListChecks size={32} className="autoprojects-gate-closed-icon" aria-hidden="true" />
      <p className="autoprojects-gate-closed-title">AutoProjects is turned off for your account.</p>
      <p className="autoprojects-gate-closed-hint">Ask an admin to enable it in Settings &gt; Features.</p>
    </div>
  )
}
