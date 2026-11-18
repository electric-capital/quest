import type { AutoProjectsRoute } from '../../utils/autoprojectsRoute'
import { AutoProjectsGateClosed } from './AutoProjectsGateClosed'
import { AutoProjectsListView } from './AutoProjectsListView'
import { NewAutoProjectPage } from './NewAutoProjectPage'
import { AutoProjectPage } from './AutoProjectPage'

/**
 * The main-pane view of a /autoprojects URL (parseAutoProjectsRoute), shared by the
 * desktop layout (App.tsx) and MobileShell, which wrap it in their
 * `main-content docs-main autoprojects-main` pane: the gate notice while the
 * `autoprojects` feature gate is closed, otherwise the list, New AutoProject, or the AutoProject
 * page. A run URL is the AutoProject page with that run open in its detail panel;
 * the page is keyed by AutoProject id only, so opening and closing a run (or
 * switching runs) keeps the page and its state, while switching AutoProjects
 * starts from a fresh one. Lasting routing glue, not a placeholder.
 */
export function AutoProjectsMain({ route, enabled }: { route: AutoProjectsRoute; enabled: boolean }) {
  if (!enabled) return <AutoProjectsGateClosed />
  switch (route.kind) {
    case 'list':
      return <AutoProjectsListView archived={route.archived} />
    case 'new':
      return <NewAutoProjectPage />
    case 'page':
      return <AutoProjectPage key={route.autoprojectId} autoprojectId={route.autoprojectId} runConversationId={null} />
    case 'run':
      return (
        <AutoProjectPage key={route.autoprojectId} autoprojectId={route.autoprojectId} runConversationId={route.conversationId} />
      )
  }
}
