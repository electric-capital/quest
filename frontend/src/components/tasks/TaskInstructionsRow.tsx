import { ChevronRight, Plus, ScrollText } from 'lucide-react'

/**
 * The compact instructions row above the brief in the task page's main
 * column. The instructions themselves are viewed and edited in the detail
 * panel (TaskInstructionsPanel); this row only says whether there are any:
 * "Instructions" with the first line of the text, or "Add instructions"
 * (which opens the panel straight into the editor). `open` marks it
 * expanded while the panel shows the instructions.
 */
export function TaskInstructionsRow({
  instructions,
  open,
  onOpen,
}: {
  instructions: string
  open: boolean
  onOpen: (startEditing: boolean) => void
}) {
  const excerpt = instructions.trim().split('\n', 1)[0]
  if (!excerpt) {
    return (
      <button
        type="button"
        className="task-instructions-row task-instructions-row--empty"
        aria-expanded={open}
        onClick={() => onOpen(true)}
      >
        <Plus size={15} className="task-instructions-row-icon" aria-hidden="true" />
        <span className="task-instructions-row-label">Add instructions</span>
        <span className="task-instructions-row-text">Standing rules every run follows</span>
      </button>
    )
  }
  return (
    <button
      type="button"
      className="task-instructions-row"
      aria-expanded={open}
      onClick={() => onOpen(false)}
    >
      <ScrollText size={15} className="task-instructions-row-icon" aria-hidden="true" />
      <span className="task-instructions-row-label">Instructions</span>
      <span className="task-instructions-row-text">{excerpt}</span>
      <ChevronRight size={15} className="task-instructions-row-chevron" aria-hidden="true" />
    </button>
  )
}
