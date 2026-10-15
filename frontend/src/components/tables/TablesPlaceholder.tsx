import type { TablesRoute } from '../../utils/tablesRoute'

/**
 * Empty main pane for the /tables routes while the All Tables list and the
 * viewer are being built (issue #110 phases B2 / B3 replace it). Renders the
 * route kind only, as a hook for tests and screenshots.
 */
export function TablesPlaceholder({ route }: { route: TablesRoute }) {
  return (
    <div
      className="tables-placeholder"
      data-route-kind={route.kind}
      data-table-id={route.kind === 'viewer' ? route.tableId : undefined}
    />
  )
}
