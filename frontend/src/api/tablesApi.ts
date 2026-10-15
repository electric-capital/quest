/**
 * API client for Quest Tables (/app/api/tables; see docs/api/tables-api.md
 * and the module docstrings of chat/tables/routes.py, history_routes.py,
 * download_routes.py and share_routes.py).
 *
 * Every JSON call goes through the shared request wrapper, so failures reject
 * with an ApiClientError carrying the server's error code (`errorCode`) and
 * the whole parsed body (`details`). Structured extras ride inside
 * `details.detail` (e.g. the current schema of a 409 stale_schema, the row
 * ids of a 400 rows_violate, the `path` of a filter error); the accessors at
 * the bottom pull them out. The one flat body is the 409 stale_update of a
 * metadata edit, `{error, message, current}`, as for docs. The download
 * route is a cookie-authed GET the browser fetches itself (`<a download>`),
 * so only its URL is built here.
 */

import { API_BASE_URL } from './config';
import {
  ApiClientError,
  apiDelete,
  apiGet,
  apiPost,
  apiPut,
  handleErrorResponse,
} from './request';
import type {
  CreateTableRequest,
  FilterNode,
  ListTableChangesResponse,
  ListTablesResponse,
  RestoreTableResponse,
  ShareTableRequest,
  SortKey,
  TableChangeDetail,
  TableColumn,
  TableDetail,
  TableListRow,
  TableRowsPage,
  TableSchemaChangeResponse,
  UpdateTableRequest,
  UpdateTableSchemaRequest,
} from './types';

/** Feature-gate name; the UI shows tables only when `enabledFeatures` has it. */
export const TABLES_FEATURE = 'tables';

/** Server-side cap on a list page (`limit` must be 1..200). */
export const TABLES_MAX_PAGE_SIZE = 200;

/** Server-side cap on a rows page (TABLE_GRID_MAX_LIMIT; above it is 400 invalid_limit). */
export const TABLE_GRID_MAX_LIMIT = 200;

/** Server-side cap on a change-log page (`limit` 1..200, default 50). */
export const TABLE_CHANGES_MAX_PAGE_SIZE = 200;

export type TableDownloadFormat = 'csv' | 'sqlite';

/** `/app/api/tables` (list + create). */
export function tablesEndpoint(): string {
  return `${API_BASE_URL}/tables`;
}

/** `/app/api/tables/{id}` (read, metadata edit, delete). */
export function tableEndpoint(id: string): string {
  return `${tablesEndpoint()}/${encodeURIComponent(id)}`;
}

/**
 * Attachment URL for `<a href download>`: `csv` (default) or `sqlite`;
 * `includeHistory` (editors only, SQLite only) adds the change log.
 */
export function tableDownloadUrl(
  id: string,
  format: TableDownloadFormat = 'csv',
  includeHistory = false,
): string {
  const history = includeHistory ? '&include_history=true' : '';
  return `${tableEndpoint(id)}/download?format=${format}${history}`;
}

export interface ListTablesOptions {
  /** List this project's tables; omitted / null = the user's own user tables. */
  projectId?: string | null;
  /**
   * List the tables other people shared with the viewer (user and project
   * tables, its own keyset stream). Not combinable with `projectId`.
   */
  shared?: boolean;
  /** Page size (1..200); omitted = the whole list, unpaged. */
  limit?: number;
  /** Opaque `next_cursor` from the previous page (`<updated_at>|<id>`). */
  cursor?: string | null;
}

/**
 * One page of tables, newest `updated_at` first. Rejects with
 * `project_not_found` (404), `invalid_cursor` or `invalid_request` (400).
 */
export function listTables(opts: ListTablesOptions = {}): Promise<ListTablesResponse> {
  return apiGet(tablesEndpoint(), {
    query: {
      project_id: opts.projectId || undefined,
      shared: opts.shared ? 'true' : undefined,
      limit: opts.limit,
      cursor: opts.cursor || undefined,
    },
  });
}

/**
 * Create a table (201) with change #1 "Created table". Rejects with column
 * spec codes (`detail.column` / `detail.field`), 409 `duplicate_title`,
 * 404 `project_not_found`, 400 `table_limit`.
 */
export function createTable(body: CreateTableRequest): Promise<TableDetail> {
  return apiPost(tablesEndpoint(), { body });
}

/** The detail (row + schema + last_write_conversation). Rejects 404 `table_not_found`. */
export function getTable(id: string): Promise<TableDetail> {
  return apiGet(tableEndpoint(id));
}

/**
 * Title / description / require_approval (owner only). A stale
 * `expected_updated_at` rejects with the FLAT 409 stale_update whose body
 * carries the current row: see isStaleUpdateError / staleUpdateCurrent.
 */
export function updateTable(id: string, body: UpdateTableRequest): Promise<TableDetail> {
  return apiPut(tableEndpoint(id), { body });
}

/**
 * Apply schema operations as one change (owner only). Rejects 409
 * `stale_schema` (staleSchemaCurrent), 400 `rows_violate` (rowsViolation),
 * 400 schema-operation codes with `detail.op_index`, 409 `duplicate_title`.
 */
export function updateTableSchema(
  id: string,
  body: UpdateTableSchemaRequest,
): Promise<TableSchemaChangeResponse> {
  return apiPut(`${tableEndpoint(id)}/schema`, { body });
}

/** Delete a table (owner only; 204, no body). */
export async function deleteTable(id: string): Promise<void> {
  const response = await fetch(tableEndpoint(id), {
    method: 'DELETE',
    credentials: 'include',
  });
  if (!response.ok) {
    await handleErrorResponse(response);
  }
}

export interface QueryTableRowsOptions {
  /** The filter DSL (columns by NAME); sent JSON-encoded as `?filter=`. */
  filter?: FilterNode | null;
  /** Up to 3 sort keys (columns by NAME); sent JSON-encoded as `?sort=`. */
  sort?: SortKey[] | null;
  /** 1..200 (TABLE_GRID_MAX_LIMIT); server default 100. */
  limit?: number;
  offset?: number;
  /** Only `total` (rows empty). */
  countOnly?: boolean;
}

/** Query-string params of GET /tables/{id}/rows (exported for tests / links). */
export function tableRowsQuery(
  opts: QueryTableRowsOptions = {},
): Record<string, string | number | undefined> {
  return {
    filter: opts.filter ? JSON.stringify(opts.filter) : undefined,
    sort: opts.sort && opts.sort.length > 0 ? JSON.stringify(opts.sort) : undefined,
    limit: opts.limit,
    offset: opts.offset,
    count_only: opts.countOnly ? 'true' : undefined,
  };
}

/**
 * One page of rows, cells keyed by column KEY. Rejects 400 with a filter
 * code (`invalid_filter`, `unknown_column`, `op_not_allowed`, ...) and
 * `detail.path` (filterErrorPath), or `invalid_limit` above 200.
 */
export function queryTableRows(
  id: string,
  opts: QueryTableRowsOptions = {},
): Promise<TableRowsPage> {
  return apiGet(`${tableEndpoint(id)}/rows`, { query: tableRowsQuery(opts) });
}

// --- History (editors only) ------------------------------------------------

/** A page of the change log, newest first; page on with `next_before_id`. */
export function listTableChanges(
  id: string,
  opts: { beforeId?: number | null; limit?: number } = {},
): Promise<ListTableChangesResponse> {
  return apiGet(`${tableEndpoint(id)}/changes`, {
    query: { limit: opts.limit, before_id: opts.beforeId ?? undefined },
  });
}

/** One change with its bounded before/after images. 404 `unknown_change`. */
export function getTableChange(id: string, changeId: number): Promise<TableChangeDetail> {
  return apiGet(`${tableEndpoint(id)}/changes/${encodeURIComponent(String(changeId))}`);
}

/** Restore the table to the state right after `changeId` (a new restore entry). */
export function restoreTable(id: string, changeId: number): Promise<RestoreTableResponse> {
  return apiPost(`${tableEndpoint(id)}/restore`, { body: { change_id: changeId } });
}

// --- Sharing (owner only) --------------------------------------------------

/**
 * Grant (or change) read / write access for one user or everyone. Upsert.
 * Returns the owner's list row with the new roster. Rejects e.g. 404
 * `user_not_found`, 409 `ambiguous_user`, 400 `cannot_share_with_owner`.
 */
export function addTableShare(id: string, body: ShareTableRequest): Promise<TableListRow> {
  return apiPost(`${tableEndpoint(id)}/shares`, { body });
}

/** Revoke one grant (404 `share_not_found`). Returns the owner's list row. */
export async function removeTableShare(id: string, shareId: number): Promise<TableListRow> {
  return apiDelete(`${tableEndpoint(id)}/shares/${encodeURIComponent(String(shareId))}`);
}

// --- Error accessors -------------------------------------------------------

/**
 * The structured `detail` object of a tables error (`{error, message,
 * ...extras}`), or null (not an ApiClientError, or a flat / string body).
 */
export function tableErrorDetail(err: unknown): Record<string, unknown> | null {
  if (!(err instanceof ApiClientError)) return null;
  const detail = err.details?.detail;
  return detail && typeof detail === 'object' ? (detail as Record<string, unknown>) : null;
}

/** True for the flat 409 stale_update of a metadata edit (updateTable). */
export function isStaleUpdateError(err: unknown): err is ApiClientError {
  return err instanceof ApiClientError && err.errorCode === 'stale_update';
}

/** The server's current list row from a stale_update error, else null. */
export function staleUpdateCurrent(err: unknown): TableListRow | null {
  if (!isStaleUpdateError(err)) return null;
  const current = err.details?.current;
  return current && typeof current === 'object' ? (current as TableListRow) : null;
}

/** True for the 409 stale_schema of a schema edit. */
export function isStaleSchemaError(err: unknown): err is ApiClientError {
  return err instanceof ApiClientError && err.errorCode === 'stale_schema';
}

/** The table's current schema and version from a stale_schema error, else null. */
export function staleSchemaCurrent(
  err: unknown,
): { schema_version: number; schema: TableColumn[] } | null {
  if (!isStaleSchemaError(err)) return null;
  const detail = tableErrorDetail(err);
  if (!detail || typeof detail.schema_version !== 'number' || !Array.isArray(detail.schema)) {
    return null;
  }
  return { schema_version: detail.schema_version, schema: detail.schema as TableColumn[] };
}

/** Extras of a 400 rows_violate: the check existing rows fail and which rows. */
export interface TableRowsViolation {
  // Column name the operation targeted.
  column: string | null;
  // The failing check, e.g. "required", "unique", "option".
  check: string | null;
  // The enum option involved, for option checks.
  option: string | null;
  // The first offending row ids, and how many there are in all.
  row_ids: number[];
  total: number | null;
}

/** The rows_violate extras of a schema-edit error, else null. */
export function rowsViolation(err: unknown): TableRowsViolation | null {
  if (!(err instanceof ApiClientError) || err.errorCode !== 'rows_violate') return null;
  const detail = tableErrorDetail(err);
  if (!detail) return null;
  const str = (value: unknown) => (typeof value === 'string' ? value : null);
  return {
    column: str(detail.column),
    check: str(detail.check),
    option: str(detail.option),
    row_ids: Array.isArray(detail.row_ids)
      ? detail.row_ids.filter((id): id is number => typeof id === 'number')
      : [],
    total: typeof detail.total === 'number' ? detail.total : null,
  };
}

/**
 * The offending node of a filter / sort / page error (`""` = the filter
 * root, else e.g. `and[1].value`, `sort[0].column`, `limit`), else null.
 */
export function filterErrorPath(err: unknown): string | null {
  const detail = tableErrorDetail(err);
  return detail && typeof detail.path === 'string' ? detail.path : null;
}
