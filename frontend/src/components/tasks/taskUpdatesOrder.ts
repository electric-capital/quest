/**
 * The viewer's sort order of a task page's updates (the Updates card's
 * "Oldest first" / "Recent first" control): one preference for every task,
 * kept in this browser's localStorage. Storage can be blocked (a private
 * window, cleared site data), so reads fall back to oldest first and writes
 * only last for the page.
 */

/** Display order of the updates (TaskUpdates' sort control). */
export type TaskUpdatesOrder = 'oldest' | 'recent'

const ORDER_STORAGE_KEY = 'quest_task_updates_order'

/** The viewer's stored sort order (one preference for every task); oldest first by default. */
export function readTaskUpdatesOrder(): TaskUpdatesOrder {
  try {
    return localStorage.getItem(ORDER_STORAGE_KEY) === 'recent' ? 'recent' : 'oldest'
  } catch {
    return 'oldest'
  }
}

export function writeTaskUpdatesOrder(order: TaskUpdatesOrder): void {
  try {
    localStorage.setItem(ORDER_STORAGE_KEY, order)
  } catch {
    // Storage blocked: the choice lasts for this page only.
  }
}
