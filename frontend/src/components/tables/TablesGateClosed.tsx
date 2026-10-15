import { Table2 } from 'lucide-react'
import './TablesGateClosed.css'

/**
 * Shown on a /tables URL while the `tables` feature gate is closed for the
 * current user (enabled_features on GET /me). Nothing is fetched: every
 * /app/api/tables route would 403 `tables_disabled` anyway.
 */
export function TablesGateClosed() {
  return (
    <div className="tables-gate-closed" role="status">
      <Table2 size={32} className="tables-gate-closed-icon" aria-hidden="true" />
      <p className="tables-gate-closed-title">Quest Tables is turned off for your account.</p>
      <p className="tables-gate-closed-hint">Ask an admin to enable it in Settings &gt; Features.</p>
    </div>
  )
}
