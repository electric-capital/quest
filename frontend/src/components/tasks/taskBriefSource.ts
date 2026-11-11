import type { TaskBriefSource } from '../../api/types'

/**
 * Labels for who wrote a task brief, from the `source` the server stores on
 * the brief and on each brief version (`run:<run id>`, `ui`,
 * `restore:<version>`), plus the writing run's `seq` when the server could
 * resolve it (null once that run was deleted). Shared by the brief's
 * attribution line, the editor's "updated while you were editing" notice
 * and the History list (TaskBrief, TaskBriefEditor, TaskBriefHistory).
 */

export type ParsedBriefSource =
  | { kind: 'run' }
  | { kind: 'ui' }
  | { kind: 'restore'; version: number }
  | { kind: 'unknown' }

export function parseBriefSource(source: TaskBriefSource): ParsedBriefSource {
  if (source === 'ui') return { kind: 'ui' }
  if (source?.startsWith('run:')) return { kind: 'run' }
  const restore = source ? /^restore:(\d+)$/.exec(source) : null
  if (restore) return { kind: 'restore', version: Number(restore[1]) }
  return { kind: 'unknown' }
}

/**
 * The current brief's attribution: "Update N" ("An update" when the run is
 * gone), "Edited by you", "Restored from version n"; null when there is no
 * (known) source, e.g. an empty brief at version 0. TaskBrief puts the time
 * first for an update ("Updated 3h ago · update 2"), see briefByline.
 */
export function briefAttribution(source: TaskBriefSource, runSeq: number | null): string | null {
  const parsed = parseBriefSource(source)
  switch (parsed.kind) {
    case 'run':
      return runSeq !== null ? `Update ${runSeq}` : 'An update'
    case 'ui':
      return 'Edited by you'
    case 'restore':
      return `Restored from version ${parsed.version}`
    default:
      return null
  }
}

/**
 * A History row's source: "Update N" ("An update" when the run is gone), "Edited
 * by you", "Restored from version n"; null for an unknown source.
 */
export function versionSourceLabel(source: TaskBriefSource, runSeq: number | null): string | null {
  const parsed = parseBriefSource(source)
  switch (parsed.kind) {
    case 'run':
      return runSeq !== null ? `Update ${runSeq}` : 'An update'
    case 'ui':
      return 'Edited by you'
    case 'restore':
      return `Restored from version ${parsed.version}`
    default:
      return null
  }
}

/**
 * Who moved the brief, for "This brief was updated <phrase> while you were
 * editing": "by update N", "by an update", "by a restore", else "elsewhere" (an
 * edit in another tab or window).
 */
export function briefWriterPhrase(source: TaskBriefSource, runSeq: number | null): string {
  const parsed = parseBriefSource(source)
  if (parsed.kind === 'run') return runSeq !== null ? `by update ${runSeq}` : 'by an update'
  if (parsed.kind === 'restore') return 'by a restore'
  return 'elsewhere'
}
