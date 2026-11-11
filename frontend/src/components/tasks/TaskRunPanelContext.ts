import { createContext } from 'react'
import type { TaskQuestion } from '../../api/types'

/**
 * What the task page's run panel (TaskRunPanel) tells the run transcript it
 * renders through ChatPanel: the task the run belongs to, so a `task_question` row
 * (TaskQuestionMessage) can post its answer, and the run's questions as the
 * runs page last listed them (fresher than the transcript row, which the
 * conversation store does not re-read when the server rewrites it). Null
 * outside the run panel: the row then renders read-only.
 */
export interface TaskRunPanelContextValue {
  taskId: string
  questions: TaskQuestion[]
}

export const TaskRunPanelContext = createContext<TaskRunPanelContextValue | null>(null)
