import { useId, useState, type ReactNode } from 'react'
import { ChevronRight } from 'lucide-react'

export interface TaskPanelSectionProps {
  /** The heading; also the section's aria-label ("Instructions", "Brief", "Task files"). */
  title: string
  /** Phones: the one-line summary shown in the collapsed row ("Updated by run 3 · 2h ago"). */
  summary?: string
  /**
   * Quiet controls (Edit, History): on the heading row on desktop, beside
   * the disclosure while the section is open on phones.
   */
  actions?: ReactNode
  /** Phones: the section is a disclosure row, collapsed until tapped. */
  collapsible: boolean
  children: ReactNode
}

/**
 * The chrome of one task panel section (TaskPanel): a small uppercase
 * heading with its controls on the right on desktop, always open. On
 * phones (`collapsible`) the heading is a full-width disclosure button with
 * a chevron, `aria-expanded`, and a one-line summary while collapsed;
 * sections start collapsed.
 *
 * The body stays mounted while collapsed (`hidden`), and sits at the same
 * place in the tree in both modes, so collapsing a section or crossing the
 * phone breakpoint never drops an editor's draft (the section components
 * keep their own state above this one anyway).
 */
export function TaskPanelSection({ title, summary, actions, collapsible, children }: TaskPanelSectionProps) {
  const [open, setOpen] = useState(false)
  const bodyId = useId()
  const expanded = !collapsible || open

  return (
    <section
      className={`task-panel-section${collapsible ? ' task-panel-section--collapsible' : ''}${expanded ? ' is-open' : ''}`}
      aria-label={title}
    >
      <div className="task-panel-section-head">
        {collapsible ? (
          <h2 className="task-panel-section-heading">
            <button
              type="button"
              className="task-panel-disclosure"
              aria-expanded={open}
              aria-controls={bodyId}
              onClick={() => setOpen((v) => !v)}
            >
              <ChevronRight size={16} className="task-panel-disclosure-chevron" aria-hidden="true" />
              <span className="task-panel-disclosure-title">{title}</span>
              {!open && summary && <span className="task-panel-disclosure-summary">{summary}</span>}
            </button>
          </h2>
        ) : (
          <h2 className="task-panel-heading">{title}</h2>
        )}
        {expanded && actions && <div className="task-panel-section-actions">{actions}</div>}
      </div>
      <div id={bodyId} className="task-panel-section-body" hidden={!expanded}>
        {children}
      </div>
    </section>
  )
}
