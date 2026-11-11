/**
 * The Sidebar "Tasks" block (main panel only, between Projects and Docs;
 * tasks are top-level, so the drill-down has none): the user's unarchived
 * tasks with the newest activity first. The whole header is a button that
 * opens the tasks list, with a "+" beside it for New Task; a row opens that
 * task's thread. Each row carries the unread dot and one status slot: a
 * working spinner while a run is running, "Needs you" while one waits on the
 * user, otherwise the last-activity time. A quiet footer links to the full
 * list (when there are more tasks than rows) and to the archived list.
 *
 * Data comes from useTasks (owned by the Sidebar, which renders this block
 * only while the `tasks` feature gate is open) and row order from
 * utils/sidebarTasks; this component only renders it. Styling reuses the
 * docs block's classes (Sidebar.css), so the blocks read alike; the
 * `tasks-section` / `task-row` classes identify this one.
 */

import { useMemo } from 'react';
import { ListChecks, LoaderCircle } from 'lucide-react';
import type { TaskListRow } from '../../api/types';
import { useTaskUnread } from '../../hooks/useTaskUnread';
import { deriveSidebarTaskItems, SIDEBAR_TASK_LIMIT } from '../../utils/sidebarTasks';
import { formatSidebarDocTime, formatSidebarDocTimeTitle } from '../../utils/sidebarDocs';
import { ChevronRightIcon, PlusIcon } from './icons';

export const EMPTY_HINT = 'No tasks yet';

export interface TasksSectionProps {
  // The unarchived tasks (useTasks().tasks), in any order.
  tasks: TaskListRow[];
  // The first load has not landed yet.
  loading: boolean;
  // useTasks().error: the first load failed (no tasks to show).
  error?: string | null;
  // The task whose page is showing (with or without a run open), so its row is highlighted.
  activeTaskId: string | null;
  onOpenTask: (id: string) => void;
  // The unarchived tasks list (the header and "Show all").
  onOpenAll: () => void;
  onOpenArchived: () => void;
  onNewTask: () => void;
  // The unarchived tasks list is the view showing.
  headerActive?: boolean;
  // The archived tasks list is the view showing.
  archivedActive?: boolean;
}

function TaskRowStatus({ task }: { task: TaskListRow }) {
  const status = task.active_run?.status;
  if (status === 'running') {
    return (
      <span className="task-row-status task-row-working" role="img" aria-label="Working" title="Working">
        <LoaderCircle size={12} aria-hidden="true" />
      </span>
    );
  }
  if (status === 'waiting') {
    return (
      <span className="task-row-status task-row-needs-you" title="A run is waiting for you">
        Needs you
      </span>
    );
  }
  if (!task.last_activity_at) return null;
  return (
    <span className="doc-row-time" title={formatSidebarDocTimeTitle(task.last_activity_at)}>
      {formatSidebarDocTime(task.last_activity_at)}
    </span>
  );
}

function TaskRow({
  task,
  active,
  onOpen,
}: {
  task: TaskListRow;
  active: boolean;
  onOpen: () => void;
}) {
  const unread = useTaskUnread(task);
  return (
    <button
      type="button"
      className={`conversation-item doc-row task-row${active ? ' active' : ''}`}
      onClick={onOpen}
      title={task.title}
      aria-current={active ? 'page' : undefined}
    >
      <ListChecks size={14} className="doc-row-icon" aria-hidden="true" />
      <span className="doc-row-title">
        {unread && (
          <span className="conversation-unread-dot" role="img" aria-label="Unread" />
        )}
        {task.title}
      </span>
      <TaskRowStatus task={task} />
    </button>
  );
}

export function TasksSection({
  tasks,
  loading,
  error = null,
  activeTaskId,
  onOpenTask,
  onOpenAll,
  onOpenArchived,
  onNewTask,
  headerActive = false,
  archivedActive = false,
}: TasksSectionProps) {
  const items = useMemo(() => deriveSidebarTaskItems(tasks), [tasks]);
  // A failed load is not "no tasks": no empty hint.
  const failed = !loading && Boolean(error) && tasks.length === 0;

  return (
    <div className="docs-section tasks-section">
      <div className="section-header docs-section-header tasks-section-header">
        <button
          type="button"
          className={`docs-section-header-button${headerActive ? ' active' : ''}`}
          onClick={onOpenAll}
          title="All tasks"
          aria-current={headerActive ? 'page' : undefined}
        >
          <span className="section-label">Tasks</span>
          <ChevronRightIcon className="docs-section-chevron" />
        </button>
        <div className="section-header-actions">
          <button
            type="button"
            className="section-add-button"
            onClick={onNewTask}
            title="New task"
            aria-label="New task"
          >
            <PlusIcon />
          </button>
        </div>
      </div>

      {items.map((task) => (
        <TaskRow
          key={task.id}
          task={task}
          active={activeTaskId === task.id}
          onOpen={() => onOpenTask(task.id)}
        />
      ))}

      {/* Quiet while the first load is pending: no "Loading..." line that
          would flash before the rows or the empty state. */}
      {failed ? (
        <div className="sidebar-empty">Couldn't load tasks</div>
      ) : (
        !loading && tasks.length === 0 && (
          <div className="sidebar-empty">{EMPTY_HINT}</div>
        )
      )}

      {!loading && (
        <div className="tasks-section-footer">
          {tasks.length > SIDEBAR_TASK_LIMIT && (
            <button type="button" className="tasks-section-link" onClick={onOpenAll}>
              Show all ({tasks.length})
            </button>
          )}
          <button
            type="button"
            className={`tasks-section-link${archivedActive ? ' active' : ''}`}
            onClick={onOpenArchived}
            aria-current={archivedActive ? 'page' : undefined}
          >
            Archived
          </button>
        </div>
      )}
    </div>
  );
}
