/**
 * Space-qualified workspace references in rendered markdown: the `chat://`,
 * `proj://` and `task://` paths the model's file tools take
 * (chat/gemini_api/tool_handlers/file_paths.py), written by the model into
 * replies, results and briefs as link targets and image sources.
 *
 * Which id each scheme resolves against is the renderer's business
 * (MarkdownLink / MarkdownImage in components/Message.tsx); this module
 * only recognises and normalises them. The path checks stay the server's:
 * nothing the routes would accept is refused here.
 */

export type WorkspaceRefScheme = 'chat' | 'proj' | 'task';

/** A parsed reference: its space and the path relative to that space's root ('' for the root). */
export interface WorkspaceRef {
  scheme: WorkspaceRefScheme;
  path: string;
}

/** `chat://`, `proj://` or `task://` (any case), then the space path. */
const WORKSPACE_REF_RE = /^(chat|proj|task):\/\/(.*)$/is;

/** Whether `url` is a space-qualified workspace reference. */
export function isWorkspaceRef(url: string): boolean {
  return WORKSPACE_REF_RE.test(url);
}

/**
 * Split a reference into its space and path: percent-decoded (markdown
 * destinations arrive encoded), with leading "/" and "./" dropped, so
 * `task://reports/q3.md`, `task:///reports/q3.md` and
 * `task://reports/q3%20final.md` all name a path under the task root.
 * Null for anything else.
 */
export function parseWorkspaceRef(url: string): WorkspaceRef | null {
  const match = WORKSPACE_REF_RE.exec(url);
  if (!match) return null;
  let path = match[2];
  try {
    path = decodeURIComponent(path);
  } catch {
    // Malformed escape: keep the raw string; the server judges the path.
  }
  path = path.replace(/^(?:\.?\/)+/, '');
  return { scheme: match[1].toLowerCase() as WorkspaceRefScheme, path };
}
