/**
 * The viewer's sort order of an AutoProject page's updates (the Updates card's
 * "Oldest first" / "Recent first" control): one preference for every AutoProject,
 * kept in this browser's localStorage. Recent first is the default: the
 * composer sits right above the Updates card, so a new update appears
 * directly under it. Storage can be blocked (a private window, cleared
 * site data), so reads fall back to the default and writes only last for
 * the page.
 */

/** Display order of the updates (AutoProjectUpdates' sort control). */
export type AutoProjectUpdatesOrder = 'oldest' | 'recent'

const ORDER_STORAGE_KEY = 'quest_autoproject_updates_order'

/**
 * The viewer's stored sort order (one preference for every AutoProject); recent
 * first by default.
 */
export function readAutoProjectUpdatesOrder(): AutoProjectUpdatesOrder {
  try {
    return localStorage.getItem(ORDER_STORAGE_KEY) === 'oldest' ? 'oldest' : 'recent'
  } catch {
    return 'recent'
  }
}

export function writeAutoProjectUpdatesOrder(order: AutoProjectUpdatesOrder): void {
  try {
    localStorage.setItem(ORDER_STORAGE_KEY, order)
  } catch {
    // Storage blocked: the choice lasts for this page only.
  }
}
