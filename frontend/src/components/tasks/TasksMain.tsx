import type { TasksRoute } from '../../utils/tasksRoute'
import { TasksGateClosed } from './TasksGateClosed'
import { TasksListView } from './TasksListView'
import { NewTaskPage } from './NewTaskPage'
import { TaskPage } from './TaskPage'

/**
 * The main-pane view of a /tasks URL (parseTasksRoute), shared by the
 * desktop layout (App.tsx) and MobileShell, which wrap it in their
 * `main-content docs-main tasks-main` pane: the gate notice while the
 * `tasks` feature gate is closed, otherwise the list, New Task, or the task
 * page. A run URL is the task page with that run open in its detail panel;
 * the page is keyed by task id only, so opening and closing a run (or
 * switching runs) keeps the page and its state, while switching tasks
 * starts from a fresh one. Lasting routing glue, not a placeholder.
 */
export function TasksMain({ route, enabled }: { route: TasksRoute; enabled: boolean }) {
  if (!enabled) return <TasksGateClosed />
  switch (route.kind) {
    case 'list':
      return <TasksListView archived={route.archived} />
    case 'new':
      return <NewTaskPage />
    case 'thread':
      return <TaskPage key={route.taskId} taskId={route.taskId} runConversationId={null} />
    case 'run':
      return (
        <TaskPage key={route.taskId} taskId={route.taskId} runConversationId={route.conversationId} />
      )
  }
}
