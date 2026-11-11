import { memo, useMemo } from 'react'
import type { FileSource } from '../../api/fileApi'
import { MarkdownWorkspaceContext, markdownComponents, type MarkdownWorkspaceContextValue } from '../Message'
import { RichMarkdown } from '../RichMarkdown'

/**
 * A brief's markdown (the current brief in the main column, or one History
 * version in the detail panel), rendered with the chat markdown typography
 * (`message-content`, its bubble chrome stripped in TaskControls.css).
 * Memoized: the parse of a long brief must not rerun on unrelated page
 * state such as typing in an editor.
 *
 * The brief belongs to the task, so its `task://` references and bare
 * relative paths resolve against the task workspace: images load inline,
 * and with `onOpenFile` (the task page's file viewer) a plain click on a
 * link or image opens the file there; without it a link opens the file's
 * download route in a new tab. Nothing else resolves: `chat://` names a
 * run's own workspace, which a brief has none of. The explicit value keeps
 * any enclosing context from leaking in.
 */
export const TaskBriefMarkdown = memo(function TaskBriefMarkdown({
  taskId,
  content,
  onOpenFile,
}: {
  taskId: string
  content: string
  onOpenFile?: (workspacePath: string, filename: string, source: FileSource) => void
}) {
  const workspace = useMemo(
    (): MarkdownWorkspaceContextValue => ({ taskId, onOpenFile }),
    [taskId, onOpenFile],
  )
  return (
    <div className="message-content task-ctl-markdown">
      <MarkdownWorkspaceContext.Provider value={workspace}>
        <RichMarkdown components={markdownComponents}>{content}</RichMarkdown>
      </MarkdownWorkspaceContext.Provider>
    </div>
  )
})
