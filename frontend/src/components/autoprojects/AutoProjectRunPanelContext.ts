import { createContext } from 'react'
import type { AutoProjectQuestion } from '../../api/types'

/**
 * What the AutoProject page's run panel (AutoProjectRunPanel) tells the run transcript it
 * renders through ChatPanel: the AutoProject the run belongs to, so an `autoproject_question` row
 * (AutoProjectQuestionMessage) can post its answer, and the run's questions as the
 * runs page last listed them (fresher than the transcript row, which the
 * conversation store does not re-read when the server rewrites it). Null
 * outside the run panel: the row then renders read-only.
 */
export interface AutoProjectRunPanelContextValue {
  autoprojectId: string
  questions: AutoProjectQuestion[]
}

export const AutoProjectRunPanelContext = createContext<AutoProjectRunPanelContextValue | null>(null)
