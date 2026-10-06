// Placeholder; replaced by Package 4/5 (P4: All Docs view). Keep the signature.

export function DocsListView({ projectId }: { projectId: string | null }) {
  return (
    <div className="docs-view-placeholder">
      {projectId ? `Project docs (${projectId})` : 'All Docs'}
    </div>
  )
}
