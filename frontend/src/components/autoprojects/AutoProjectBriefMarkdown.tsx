import { memo, useMemo } from 'react'
import type { FileSource } from '../../api/fileApi'
import { MarkdownWorkspaceContext, markdownComponents, type MarkdownWorkspaceContextValue } from '../Message'
import { RichMarkdown } from '../RichMarkdown'

/**
 * An AutoProject's markdown: a brief (the current brief in the main column,
 * or one History version in the detail panel), or with `conversationId` a
 * run's result or handoff notes; rendered with the chat markdown typography
 * (`message-content`, its bubble chrome stripped in AutoProjectBriefControls.css;
 * `className` adds to it). Memoized: the parse of a long brief must not
 * rerun on unrelated page state such as typing in an editor.
 *
 * `autoproject://` references resolve against the AutoProject workspace, and so do
 * bare relative paths in a brief; with `conversationId` (a run's text) bare
 * paths and `chat://` resolve against that run's workspace instead. Images
 * load inline, and with `onOpenFile` (the AutoProject page's file viewer) a
 * plain click on a link or image opens the file there; without it a link
 * opens the file's download route in a new tab. Without `conversationId`
 * `chat://` does not resolve: a brief has no run workspace. The explicit
 * value keeps any enclosing context from leaking in.
 */
export const AutoProjectBriefMarkdown = memo(function AutoProjectBriefMarkdown({
  autoprojectId,
  conversationId,
  content,
  className,
  onOpenFile,
}: {
  autoprojectId: string
  /** The run whose result or notes this is. */
  conversationId?: string
  content: string
  className?: string
  onOpenFile?: (workspacePath: string, filename: string, source: FileSource) => void
}) {
  const workspace = useMemo(
    (): MarkdownWorkspaceContextValue => ({ conversationId, autoprojectId, onOpenFile }),
    [conversationId, autoprojectId, onOpenFile],
  )
  return (
    <div className={`message-content autoproject-ctl-markdown${className ? ` ${className}` : ''}`}>
      <MarkdownWorkspaceContext.Provider value={workspace}>
        <RichMarkdown components={markdownComponents}>{content}</RichMarkdown>
      </MarkdownWorkspaceContext.Provider>
    </div>
  )
})
