import { useCallback, useEffect, useId, useMemo, useRef, useState, type KeyboardEvent } from 'react'
import { ChevronLeft, FileDiff, RotateCcw, X } from 'lucide-react'
import { ApiClientError } from '../../api/request'
import {
  getTaskBriefVersion,
  isStaleBriefError,
  listTaskBriefVersions,
  restoreTaskBriefVersion,
} from '../../api/tasksApi'
import type { Task, TaskBriefState, TaskBriefVersion } from '../../api/types'
import { boundedWordDiff, type WordDiffSegment } from '../../utils/wordDiff'
import { DocConfirmDialog } from '../docs/DocConfirmDialog'
import { TaskBriefMarkdown } from './TaskBriefMarkdown'
import { versionSourceLabel } from './taskBriefSource'
import { TaskTimeAgo, taskSaveErrorMessage } from './TaskControls'

// Mirrors TASK_BRIEF_MAX_VERSIONS (chat/tasks/constants.py).
const RETENTION_NOTE = 'The last 50 versions of the brief are kept.'
const STALE_NOTICE =
  'The brief changed meanwhile; the list was refreshed, check the version and try again.'
const GONE_NOTICE = 'That version is no longer available.'

/**
 * Word-diff table budget. A 12,000-character brief is about 2,000 words,
 * so two full rewrites of a maximal brief still fit; beyond it the diff is
 * refused rather than freezing the tab.
 */
const DIFF_MAX_CELLS = 4_000_000

/** A failed version fetch, for the version it was for. */
interface DetailError {
  version: number
  message: string
  /** False for a version that is gone (retrying cannot help). */
  retryable: boolean
}

/** The version a Restore confirm is for and the current version it replaces, frozen at open. */
interface RestoreTarget {
  version: number
  expected: number
}

function isVersionGone(err: unknown): boolean {
  return (
    err instanceof ApiClientError &&
    err.statusCode === 404 &&
    err.errorCode === 'brief_version_not_found'
  )
}

/**
 * The word diff from a version to the current brief, rendered like the
 * memory suggestions' ReplacementDiff: the raw markdown pre-wrap, removed
 * words struck on red, added words on green.
 */
function BriefDiff({ before, after }: { before: string; after: string }) {
  const segments = useMemo<WordDiffSegment[] | null>(
    () => boundedWordDiff(before, after, DIFF_MAX_CELLS),
    [before, after],
  )
  if (segments === null) {
    return <p className="task-ctl-muted">Too many changes to show a word diff.</p>
  }
  if (!segments.some((s) => s.type !== 'equal' && /\S/.test(s.text))) {
    return <p className="task-ctl-muted">Same as the current brief.</p>
  }
  return (
    <section className="task-brief-diff" aria-label="Changes since this version">
      <p className="task-brief-diff-legend">
        Words marked <span className="task-brief-diff-add">green</span> were added since this version;
        struck <span className="task-brief-diff-del">red</span> ones were removed.
      </p>
      <div className="task-brief-diff-body">
        {segments.map((segment, i) => {
          if (segment.type === 'equal') return <span key={i}>{segment.text}</span>
          // The whitespace before a changed word is spacing, not a change.
          const [, lead, text] = /^(\s*)([\s\S]*)$/.exec(segment.text)!
          const Tag = segment.type === 'del' ? 'del' : 'ins'
          return (
            <span key={i}>
              {lead}
              <Tag className={`task-brief-diff-${segment.type}`}>{text}</Tag>
            </span>
          )
        })}
      </div>
    </section>
  )
}

/**
 * The brief's History, in the task page's detail panel (TaskPage wraps it
 * in TaskDetailPanel, titled "Brief history", beside the current brief in
 * the main column): DocHistory's phone pattern, a version list, and one
 * version's detail with an "All versions" control back to the list
 * (Escape in the detail goes back to the list too; Escape on the list is
 * the panel's, which closes).
 *
 * List: `listTaskBriefVersions`, newest first: "Version N", its source
 * ("Run N", "Edited by you", "Restored from version n"), when it was
 * written, and the excerpt; the row whose version is `task.brief_version`
 * is marked Current. Fetched on open and again ONLY when
 * `task.brief_version` moves while History is open (every write adds a
 * version), plus explicit reloads after a refused restore. A selected
 * version that drops out of a refreshed list (pruned) is reported gone.
 *
 * Detail: the version's text (`getTaskBriefVersion`, cached by version:
 * versions are immutable; the current version uses `task.brief`), rendered
 * as markdown, or with "Show changes" a word diff from that version to the
 * current brief (BriefDiff). Restore (not on the current version, and only
 * once its text has loaded) opens a confirm; confirming sends the
 * `brief_version` frozen when the confirm opened, so a write landing
 * behind the dialog is a 409, never a silent overwrite. Success hands the
 * new brief state to `onRestored` (the page applies it, so the main column
 * shows the restored brief) and goes back to the list with a note naming
 * the new version; 409 `stale_brief` closes the dialog with a notice, calls
 * `onStale` (useTask's refresh) and reloads the list; 404
 * `brief_version_not_found` closes it with a notice and reloads the list;
 * other errors stay inline in the dialog. Restoring while a run is active
 * is allowed (the run merges, as for an edit).
 *
 * List, detail and restore responses are sequence-numbered: one whose
 * number is no longer current is dropped. Focus: the panel's heading on
 * open (TaskDetailPanel), "All versions" on opening a version, the
 * selected row on going back.
 */
export function TaskBriefHistory({
  task,
  onRestored,
  onStale,
}: {
  task: Task
  onRestored: (brief: TaskBriefState) => void
  onStale: () => void
}) {
  const taskId = task.id
  const currentVersion = task.brief_version
  const ids = useId()
  const rootRef = useRef<HTMLElement>(null)
  const listRef = useRef<HTMLUListElement>(null)
  const allVersionsRef = useRef<HTMLButtonElement>(null)
  const restoreButtonRef = useRef<HTMLButtonElement>(null)
  const pendingFocusRef = useRef<'detail' | 'list' | null>(null)

  const [notice, setNotice] = useState<string | null>(null)

  // --- Version list -----------------------------------------------------------
  const [versions, setVersions] = useState<TaskBriefVersion[] | null>(null)
  const [listError, setListError] = useState<string | null>(null)
  const [listReload, setListReload] = useState(0)
  const listSeqRef = useRef(0)
  const reloadList = useCallback(() => setListReload((n) => n + 1), [])

  const [selected, setSelected] = useState<number | null>(null)
  const selectedRef = useRef<number | null>(null)
  selectedRef.current = selected

  useEffect(() => {
    const seq = ++listSeqRef.current
    setListError(null)
    listTaskBriefVersions(taskId).then(
      (response) => {
        if (seq !== listSeqRef.current) return
        setVersions(response.versions)
        const picked = selectedRef.current
        if (picked !== null && !response.versions.some((v) => v.version === picked)) {
          setSelected(null)
          setNotice(GONE_NOTICE)
        }
      },
      (err: unknown) => {
        if (seq !== listSeqRef.current) return
        setListError(taskSaveErrorMessage(err, 'Failed to load the versions.'))
      },
    )
    // currentVersion: every brief write adds a version.
  }, [taskId, currentVersion, listReload])

  // --- Selected version's text --------------------------------------------------
  const [bodies, setBodies] = useState<Record<number, string>>({})
  const [detailError, setDetailError] = useState<DetailError | null>(null)
  const [detailRetry, setDetailRetry] = useState(0)
  const [showChanges, setShowChanges] = useState(false)
  const bodySeqRef = useRef(0)

  const isCurrent = selected !== null && selected === currentVersion
  const body: string | undefined =
    selected === null ? undefined : isCurrent ? task.brief : bodies[selected]
  const needBody = selected !== null && !isCurrent && bodies[selected] === undefined

  useEffect(() => {
    const seq = ++bodySeqRef.current
    if (selected === null || !needBody) return
    const version = selected
    setDetailError(null)
    getTaskBriefVersion(taskId, version).then(
      // Immutable: cached whatever is selected by the time it lands.
      (detail) => setBodies((prev) => ({ ...prev, [version]: detail.content })),
      (err: unknown) => {
        if (seq !== bodySeqRef.current) return
        if (isVersionGone(err)) {
          setDetailError({ version, message: GONE_NOTICE, retryable: false })
          reloadList()
        } else {
          setDetailError({
            version,
            message: taskSaveErrorMessage(err, 'Failed to load this version.'),
            retryable: true,
          })
        }
      },
    )
  }, [taskId, selected, needBody, detailRetry, reloadList])

  // --- Restore --------------------------------------------------------------------
  const [restoreTarget, setRestoreTarget] = useState<RestoreTarget | null>(null)
  const [restoreBusy, setRestoreBusy] = useState(false)
  const [restoreError, setRestoreError] = useState<string | null>(null)
  const restoreSeqRef = useRef(0)

  const openRestore = (version: number) => {
    setNotice(null)
    setRestoreError(null)
    setRestoreTarget({ version, expected: currentVersion })
  }

  const closeRestore = useCallback(() => {
    restoreSeqRef.current += 1
    setRestoreTarget(null)
    setRestoreError(null)
    setRestoreBusy(false)
    // Back to the trigger (gone with the version: the first focusable control).
    setTimeout(
      () => (restoreButtonRef.current ?? rootRef.current?.querySelector<HTMLElement>('button'))?.focus(),
      0,
    )
  }, [])

  const confirmRestore = async () => {
    const target = restoreTarget
    if (target === null || restoreBusy) return
    const seq = ++restoreSeqRef.current
    setRestoreBusy(true)
    setRestoreError(null)
    try {
      const result = await restoreTaskBriefVersion(taskId, target.version, target.expected)
      if (seq !== restoreSeqRef.current) return
      setRestoreBusy(false)
      setRestoreTarget(null)
      onRestored(result)
      pendingFocusRef.current = 'list'
      setSelected(null)
      setNotice(`Version ${target.version} was restored as version ${result.brief_version}.`)
    } catch (err) {
      if (seq !== restoreSeqRef.current) return
      setRestoreBusy(false)
      if (isStaleBriefError(err)) {
        closeRestore()
        setNotice(STALE_NOTICE)
        onStale()
        reloadList()
      } else if (isVersionGone(err)) {
        closeRestore()
        setNotice(GONE_NOTICE)
        reloadList()
      } else {
        setRestoreError(taskSaveErrorMessage(err, 'Failed to restore this version.'))
      }
    }
  }

  // --- Navigation and focus ----------------------------------------------------------
  useEffect(() => {
    const target = pendingFocusRef.current
    pendingFocusRef.current = null
    if (target === 'detail') allVersionsRef.current?.focus()
    else if (target === 'list') {
      listRef.current
        ?.querySelector<HTMLButtonElement>('.task-brief-history-item-select[data-selected="true"]')
        ?.focus()
    }
  }, [selected])

  const lastSelectedRef = useRef<number | null>(null)
  const openVersion = (version: number) => {
    pendingFocusRef.current = 'detail'
    lastSelectedRef.current = version
    setShowChanges(false)
    setSelected(version)
  }

  const backToList = () => {
    pendingFocusRef.current = 'list'
    setSelected(null)
  }

  const dialogOpen = restoreTarget !== null
  const onRootKeyDown = (e: KeyboardEvent<HTMLElement>) => {
    // The dialog is portaled but its key events bubble through here; its
    // own Escape closes it, not History.
    if (e.key !== 'Escape' || e.defaultPrevented || dialogOpen || selected === null) return
    e.preventDefault()
    backToList()
  }

  // Up / Down (and Home / End) move between the list rows.
  const onListKeyDown = (e: KeyboardEvent<HTMLUListElement>) => {
    if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(e.key)) return
    const buttons = Array.from(
      e.currentTarget.querySelectorAll<HTMLButtonElement>('.task-brief-history-item-select'),
    )
    const index = buttons.indexOf(document.activeElement as HTMLButtonElement)
    if (index === -1) return
    e.preventDefault()
    let next = index
    if (e.key === 'ArrowDown') next = Math.min(buttons.length - 1, index + 1)
    else if (e.key === 'ArrowUp') next = Math.max(0, index - 1)
    else if (e.key === 'Home') next = 0
    else next = buttons.length - 1
    buttons[next].focus()
  }

  // --- Render ----------------------------------------------------------------------------
  const selectedRow = selected === null ? null : versions?.find((v) => v.version === selected) ?? null

  let content
  if (versions === null) {
    content =
      listError === null ? (
        <p className="task-ctl-muted">Loading versions...</p>
      ) : (
        <div className="task-brief-history-state">
          <p className="task-ctl-error">{listError}</p>
          <button type="button" className="task-ctl-button" onClick={reloadList}>
            Retry
          </button>
        </div>
      )
  } else if (selected === null || selectedRow === null) {
    content = (
      <>
        {versions.length === 0 ? (
          <p className="task-ctl-muted">No versions yet.</p>
        ) : (
          <ul ref={listRef} className="task-brief-history-list" aria-label="Versions" onKeyDown={onListKeyDown}>
            {versions.map((row) => {
              const current = row.version === currentVersion
              const source = versionSourceLabel(row.source, row.run_seq)
              const metaId = `${ids}-meta-${row.version}`
              return (
                <li key={row.version} className="task-brief-history-item">
                  <button
                    type="button"
                    className="task-brief-history-item-select"
                    data-selected={row.version === lastSelectedRef.current ? 'true' : undefined}
                    aria-describedby={metaId}
                    onClick={() => openVersion(row.version)}
                  >
                    <span className="task-brief-history-item-title">Version {row.version}</span>
                    {current && <span className="task-ctl-badge">Current</span>}
                  </button>
                  <div id={metaId} className="task-brief-history-item-meta">
                    <span>
                      {source && (
                        <>
                          {source}
                          <span aria-hidden="true"> · </span>
                        </>
                      )}
                      <TaskTimeAgo iso={row.created_at} />
                    </span>
                    {row.excerpt && <span className="task-brief-history-item-excerpt">{row.excerpt}</span>}
                  </div>
                </li>
              )
            })}
          </ul>
        )}
        <p className="task-brief-history-retention">{RETENTION_NOTE}</p>
      </>
    )
  } else {
    const version = selectedRow.version
    const error = detailError?.version === version ? detailError : null
    const gone = error !== null && !error.retryable
    const source = versionSourceLabel(selectedRow.source, selectedRow.run_seq)

    let detailBody
    if (body !== undefined) {
      if (showChanges && !isCurrent) detailBody = <BriefDiff before={body} after={task.brief} />
      else if (body.trim() === '') detailBody = <p className="task-ctl-muted">This version is empty.</p>
      else detailBody = <TaskBriefMarkdown taskId={taskId} content={body} />
    } else if (error) {
      detailBody = (
        <div className="task-brief-history-state">
          <p className="task-ctl-error">{error.message}</p>
          {error.retryable && (
            <button type="button" className="task-ctl-button" onClick={() => setDetailRetry((n) => n + 1)}>
              Retry
            </button>
          )}
        </div>
      )
    } else {
      detailBody = <p className="task-ctl-muted">Loading this version...</p>
    }

    content = (
      <article className="task-brief-history-detail" aria-label={`Version ${version}`}>
        <button
          ref={allVersionsRef}
          type="button"
          className="task-ctl-button task-ctl-button--plain"
          onClick={backToList}
        >
          <ChevronLeft size={15} aria-hidden="true" />
          All versions
        </button>
        <div className="task-brief-history-detail-head">
          <h3 className="task-brief-history-detail-title">
            Version {version}
            {isCurrent && <span className="task-ctl-badge">Current</span>}
          </h3>
          <p className="task-brief-history-detail-meta">
            {source && (
              <>
                {source}
                <span aria-hidden="true"> · </span>
              </>
            )}
            <TaskTimeAgo iso={selectedRow.created_at} />
          </p>
        </div>
        {!isCurrent && body !== undefined && !gone && (
          <div className="task-brief-history-actions">
            <button
              type="button"
              className="task-ctl-button"
              aria-pressed={showChanges}
              onClick={() => setShowChanges((on) => !on)}
            >
              <FileDiff size={13} aria-hidden="true" />
              Show changes
            </button>
            <button
              ref={restoreButtonRef}
              type="button"
              className="task-ctl-button task-ctl-button--primary"
              onClick={() => openRestore(version)}
            >
              <RotateCcw size={13} aria-hidden="true" />
              Restore
            </button>
          </div>
        )}
        <div className="task-brief-history-detail-body">{detailBody}</div>
      </article>
    )
  }

  return (
    <section ref={rootRef} className="task-brief-history" aria-label="Versions of the brief" onKeyDown={onRootKeyDown}>
      <div aria-live="polite">
        {notice && (
          <div className="task-ctl-note">
            <span>{notice}</span>
            <button
              type="button"
              className="task-ctl-note-dismiss"
              aria-label="Dismiss"
              onClick={() => setNotice(null)}
            >
              <X size={13} aria-hidden="true" />
            </button>
          </div>
        )}
        {versions !== null && listError !== null && (
          <div className="task-ctl-note">
            <span>Couldn't refresh the versions: {listError}</span>
            <button type="button" className="task-ctl-link-button" onClick={reloadList}>
              Retry
            </button>
          </div>
        )}
      </div>
      {content}
      <DocConfirmDialog
        isOpen={restoreTarget !== null}
        title={restoreTarget ? `Restore version ${restoreTarget.version}?` : 'Restore this version?'}
        confirmLabel="Restore"
        busyLabel="Restoring..."
        busy={restoreBusy}
        error={restoreError}
        onConfirm={() => void confirmRestore()}
        onClose={closeRestore}
      >
        <p>
          Its text becomes the brief again, as a new version. The current brief
          {restoreTarget ? ` (version ${restoreTarget.expected})` : ''} is kept in History, so you can undo this.
        </p>
        {task.active_run && (
          <p>Run {task.active_run.seq} is working; if it also rewrites the brief, it will merge this.</p>
        )}
      </DocConfirmDialog>
    </section>
  )
}
