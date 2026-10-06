import { FileText } from 'lucide-react'
import './DocsGateClosed.css'

/**
 * Shown on a /docs URL while the `docs` feature gate is closed for the
 * current user (enabled_features on GET /me). Nothing is fetched: every
 * /app/api/docs route would 403 `docs_disabled` anyway.
 */
export function DocsGateClosed() {
  return (
    <div className="docs-gate-closed" role="status">
      <FileText size={32} className="docs-gate-closed-icon" aria-hidden="true" />
      <p className="docs-gate-closed-title">Quest Docs is turned off for your account.</p>
      <p className="docs-gate-closed-hint">Ask an admin to enable it in Settings &gt; Features.</p>
    </div>
  )
}
