import { memo, useMemo } from 'react'
import { MarkdownWorkspaceContext, markdownComponents, type MarkdownWorkspaceContextValue } from '../Message'
import { RichMarkdown } from '../RichMarkdown'

/**
 * A brief's markdown (the current brief, or one History version), rendered
 * with the chat markdown typography (`message-content`, its bubble chrome
 * stripped in TaskPanel.css). Memoized: the parse of a long brief must not
 * rerun on unrelated panel state such as typing in an editor.
 *
 * The brief belongs to the task, so its `task://` references and bare
 * relative paths resolve against the task workspace (links open the file's
 * download route in a new tab; images load inline). Nothing else resolves:
 * `chat://` names a run's own workspace, which a brief has none of. The
 * explicit value keeps any enclosing context from leaking in.
 */
export const TaskBriefMarkdown = memo(function TaskBriefMarkdown({
  taskId,
  content,
}: {
  taskId: string
  content: string
}) {
  const workspace = useMemo((): MarkdownWorkspaceContextValue => ({ taskId }), [taskId])
  return (
    <div className="message-content task-panel-markdown">
      <MarkdownWorkspaceContext.Provider value={workspace}>
        <RichMarkdown components={markdownComponents}>{content}</RichMarkdown>
      </MarkdownWorkspaceContext.Provider>
    </div>
  )
})
