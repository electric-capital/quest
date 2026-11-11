import { memo } from 'react'
import { MarkdownWorkspaceContext, markdownComponents, type MarkdownWorkspaceContextValue } from '../Message'
import { RichMarkdown } from '../RichMarkdown'

// No workspace: a brief's relative links and images do not resolve to any
// space (task:// references are B5's task file source). An explicit empty
// value keeps any enclosing context from leaking in.
const NO_WORKSPACE: MarkdownWorkspaceContextValue = {}

/**
 * A brief's markdown (the current brief, or one History version), rendered
 * with the chat markdown typography (`message-content`, its bubble chrome
 * stripped in TaskPanel.css). Memoized: the parse of a long brief must not
 * rerun on unrelated panel state such as typing in an editor.
 */
export const TaskBriefMarkdown = memo(function TaskBriefMarkdown({ content }: { content: string }) {
  return (
    <div className="message-content task-panel-markdown">
      <MarkdownWorkspaceContext.Provider value={NO_WORKSPACE}>
        <RichMarkdown components={markdownComponents}>{content}</RichMarkdown>
      </MarkdownWorkspaceContext.Provider>
    </div>
  )
})
