import type { TasksRoute } from '../../utils/tasksRoute'
import { TasksGateClosed } from './TasksGateClosed'
import { TasksListView } from './TasksListView'
import { NewTaskPage } from './NewTaskPage'
import { TaskPage } from './TaskPage'
import { TaskRunView } from './TaskRunView'

/**
 * The main-pane view of a /tasks URL (parseTasksRoute), shared by the
 * desktop layout (App.tsx) and MobileShell, which wrap it in their
 * `main-content docs-main tasks-main` pane: the gate notice while the
 * `tasks` feature gate is closed, otherwise the list, New Task, the thread
 * (keyed by task id, so switching tasks starts from a fresh state) or the
 * run view. Lasting routing glue, not a placeholder.
 */
export function TasksMain({ route, enabled }: { route: TasksRoute; enabled: boolean }) {
  if (!enabled) return <TasksGateClosed />
  switch (route.kind) {
    case 'list':
      return <TasksListView archived={route.archived} />
    case 'new':
      return <NewTaskPage />
    case 'thread':
      return <TaskPage key={route.taskId} taskId={route.taskId} />
    case 'run':
      return <TaskRunView taskId={route.taskId} conversationId={route.conversationId} />
  }
}
