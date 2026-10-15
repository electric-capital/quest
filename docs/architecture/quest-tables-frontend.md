# Quest Tables Frontend

## Overview

The UI at `/tables`: the sidebar Tables blocks, All Tables, and the viewer with its header menu, filter bar, server-sorted and paged grid and footer. It follows the [Quest Docs frontend](quest-docs.md#frontend) file for file (`useTables` is `useDocs`, `TablesListView` is `DocsListView`, and so on), so this doc covers only the files and the rules that span them; each component's header comment describes its own behaviour. The routes it calls are [Quest Tables API -- HTTP Routes](../api/tables-api.md#http-routes), the events [Quest Tables -- Realtime Events](quest-tables.md#realtime-events). Paths are under `frontend/src/` unless noted.

## Key Files

| File | Role |
|------|------|
| `api/tablesApi.ts` | Client for `/app/api/tables*`, the download URL builder, `TABLES_FEATURE`, and accessors for the structured error extras (the flat 409 `stale_update`, `filterErrorPath()`) |
| `api/types.ts` | `TableListRow`, `TableDetail`, `TableColumn`, `TableAccess`, the filter and sort types, `TableRowsPage` |
| `utils/tablesRoute.ts` | `parseTablesRoute()` and the tables path and query builders |
| `hooks/useTables.ts` / `hooks/useProjectTablesIndex.ts` / `hooks/useTable.ts` / `hooks/useTableRows.ts` | One keyset-paged list; the per-project fan-out for All Tables; one table's detail for the viewer; one grid page |
| `components/sidebar/TablesSection.tsx` + `utils/allTablesGrouping.ts` | The sidebar Tables blocks (main panel and project drill-down), and the pure grouping, ordering and dedupe shared with All Tables |
| `components/tables/TablesListView.tsx` | All Tables |
| `components/tables/TableViewer.tsx`, `TableHeader.tsx`, `TableGrid.tsx` + `utils/tableCells.tsx`, `TableFooter.tsx` | The viewer, its title unit and menu, the rows grid and its typed cells, the last-writer line |
| `components/tables/TableFilterBar.tsx` + `utils/tableFilter.ts` | The filter chips and the pure builder: DSL mapping, validation, URL encoding |
| `components/tables/TablesGateClosed.tsx` | Shown on a `/tables*` URL while the gate is closed |
| `App.tsx`, `components/MobileShell.tsx`, `components/Sidebar.tsx`, `components/sidebar/ProjectPanel.tsx` | Route rendering and sidebar mounting |

## Routes and State

`/tables` is All Tables, `/tables?project=<id>` the same view filtered to one project, and `/tables/<id>` the viewer, which also takes `?filter=<JSON>&sort=<JSON>`. As for docs, the URL is the only state: `App.tsx`, `MobileShell.tsx` and the Sidebar run `parseTablesRoute()` on every render, and a tables route is a full main-pane takeover with no RightPanel. While `tables` is missing from `enabledFeatures` the route renders `TablesGateClosed` and nothing is fetched, and the Sidebar mounts no Tables blocks. `MobileShell` closes its drawers when the route arrives at a list or a table, but not when only the viewer's filter or sort changes. `serve_spa_tables` in `quest.py` serves the deep links ([Quest Tables API -- SPA Routes](../api/tables-api.md#spa-routes)).

## Lists

The sidebar shows the user's own user tables and, while drilled into a project, that project's tables, each block under its Docs block and capped at `SIDEBAR_TABLE_LIMIT`. All Tables combines the same three sources as All Docs, because `GET /tables` has no cross-project stream: "Your tables" and "Shared with you" are two server-paged `useTables` streams, and each project gets a group from `useProjectTablesIndex`, archived projects included. The project filter shows only that project's stream. New Table renders disabled while the host passes no `onNewTable`.

## Viewer

`TableViewer.tsx` is keyed by table id; its only own state is the grid offset, which is not in the URL, so a link opens on the first page. Rules that span its parts:

- **Filter and sort live in the URL.** `TableFilterBar` turns the `filter` param into one chip per AND-ed condition, by column name; it and the grid's sort headers report every change back, and the viewer navigates (with `replace: true`) to the encoded DSL (`tableViewerSearch()`), so a filtered, sorted view is a link and the params are the rows API's own JSON. A filter the bar cannot show as chips (`or`, `not`, nesting, an op or value it does not offer) stays applied as one "Custom filter" chip, never dropped. A server filter refusal is shown on the bar, on the chip its `path` points at. The bar's caps in `utils/tableFilter.ts` hand-mirror the `TABLE_FILTER_*` and `TABLE_SORT_MAX_KEYS` constants in `chat/tables/constants.py`.
- **Stale column names are not sent.** A sort key or filter naming a column the loaded schema lacks (dropped or renamed) is left out rather than sent into a 400 `unknown_column`, and a note under the bar says so, with a Clear button.
- **Grid.** One page of `TABLE_GRID_PAGE_SIZE` rows (the server's `TABLE_GRID_MAX_LIMIT`), sorted server-side; a header click cycles its column through ascending, descending and none and replaces the whole sort with that one key (a linked sort of up to three keys is still sent in full, with only the first key's arrow shown); a sort or filter change returns to the first page, and a total that shrinks below the offset steps back to the last page.
- **Cells** (`utils/tableCells.tsx`). Numbers show at the column's precision by string arithmetic rounding half-even like the server, with no `Intl` and no separators, so the grid shows exactly the digits the model and the CSV see in every locale. A url becomes a link (`rel="noopener noreferrer"`) only when it parses as http(s), otherwise it renders as text. A null renders as a muted NULL marker, distinct from an empty string.
- **Header actions follow the detail's `access` flags** (`TableHeader.tsx`), never a role recomputed client-side. Columns, History and Share are disabled menu slots while the viewer passes no handler. The require-approval switch sends no `expected_updated_at`, because row writes move `updated_at` and switching approval on to stop an agent mid-write must not fail as stale. Rename sends it, and on a `stale_update` whose current row still has the old title (only rows moved) retries once against the current token.
- **Footer.** `tableLastWriter()` in `TableFooter.tsx` derives "Last written by" from `last_write_source`, which the API sends to the owner only.

## Realtime Consumers

`useTables` and `useProjectTablesIndex` re-fetch silently on `table_list_changed` behind a trailing debounce (`TABLES_REFRESH_DEBOUNCE_MS`, the fan-out's `TABLES_INDEX_REFRESH_DEBOUNCE_MS` the longer one), so a burst of writes to a table shared with everyone costs one re-fetch per list. `useTable` ignores that broadcast and follows `table_changed` for its own table only: it re-fetches the detail unless it already shows the event's `updated_at` and `schema_version`, which a null never matches, so a delete or revoked share reaches the viewer as its not-found state. An event arriving while a fetch is in flight runs one more fetch when it lands instead of superseding it, which under a burst of writes would drop every response.

A `table_changed` with an `updated_at` also re-queries the grid page, since a row write moves `updated_at` but the detail does not carry rows. An event whose `schema_version` differs from the loaded schema waits for the detail re-fetch instead, because old column names would 400 and the rows would be keyed by columns the loaded schema lacks; a rows page reporting a newer `schema_version` than the detail asks `useTable` to re-fetch. `useTableRows` coalesces refreshes the same way and keeps the last page on screen during a refetch, so a burst of agent writes cannot starve the grid.
