import type { ReactNode } from 'react'
import './TaskPanel.css'
import type { TaskView } from '../../hooks/useTask'
import { useIsMobile } from '../../hooks/useIsMobile'
import { TaskBriefSection } from './TaskBriefSection'
import { TaskInstructionsSection } from './TaskInstructionsSection'
import { TaskPanelSection } from './TaskPanelSection'

export interface TaskPanelProps {
  /** The page's one useTask view: the task, plus applyDetail / applyBrief / refresh. */
  task: TaskView
  /**
   * The Task files card (phase B5: a FileBrowser bound to the task space).
   * Rendered as the panel's last section, headed "Task files", with the same
   * section chrome as the others (a disclosure on phones); nothing is
   * rendered for it while it is not passed.
   */
  filesSection?: ReactNode
  /** Phones: the collapsed Task files row's one-line summary (e.g. "4 files"). */
  filesSummary?: string
}

/**
 * The brief and instructions panel of the task page (spec 8.3), in the
 * place the RightPanel takes beside a chat: TaskInstructionsSection, then
 * TaskBriefSection (rendered brief, editor with conflict merge, History
 * with word diff and restore), then the optional `filesSection` slot.
 *
 * Desktop: the sections are always open, each under a small uppercase
 * heading with its quiet controls on the heading row; TaskPage lays the
 * panel out as the 340px right column that scrolls on its own. Phones
 * (useIsMobile): each section is a full-width disclosure row, collapsed by
 * default with a one-line summary, and TaskPage puts the panel between the
 * header and the thread. Section state (drafts, History) survives
 * collapsing and the breakpoint switch (TaskPanelSection).
 *
 * Saves merge their responses into the page's view (`applyDetail` for the
 * instructions PATCH, `applyBrief` for brief PUT and restore), so the panel
 * shows the result without waiting for the task_changed refetch. Styles in
 * TaskPanel.css.
 */
export function TaskPanel({ task: view, filesSection, filesSummary }: TaskPanelProps) {
  const isMobile = useIsMobile()
  const task = view.task
  if (!task) return null

  return (
    <div className="task-panel">
      <TaskInstructionsSection task={task} onSaved={view.applyDetail} collapsible={isMobile} />
      <TaskBriefSection
        task={task}
        onBrief={view.applyBrief}
        onStale={() => void view.refresh()}
        collapsible={isMobile}
      />
      {filesSection !== undefined && filesSection !== null && (
        <TaskPanelSection title="Task files" summary={filesSummary} collapsible={isMobile}>
          {filesSection}
        </TaskPanelSection>
      )}
    </div>
  )
}
