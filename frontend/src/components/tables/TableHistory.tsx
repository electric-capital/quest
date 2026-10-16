/**
 * TableHistory -- the table viewer's History drawer (spec 8.4): a right-side
 * panel over the viewer listing the table's change log, the selected
 * change's detail, and "Restore table to this point".
 *
 * History is for editors (owner or write share): every history route 403s
 * `forbidden` for anyone else, since before images can hold rows the owner
 * deleted before sharing. Access lost while the drawer is open -- a 403 or
 * a 404 `table_not_found` (deleted or unshared) from any call, or
 * `table.access.can_edit` turning false -- replaces its content (and closes
 * the confirm) with a "no longer have edit access" state. A new `table.id`
 * resets the drawer (selection, details, lost access, confirm).
 *
 * List (useTableChanges: newest first, keyset "Load more", re-listed on this
 * table's `table_changed`; a failed first load offers Retry, which shows the
 * loading state again). Each entry shows `#id`, its relative time (full
 * local time on hover; the log sends zoned UTC timestamps, `+00:00`), a kind
 * badge (Rows / Schema / Restore), the summary, who made it (`source_label`,
 * linked to the conversation when the server resolved one -- owners only;
 * plus the actor when the label does not already name them, see
 * `actorLabel`) and, for a rows change, its row count. A restore entry reads
 * "Restored to #<n>", and its meta line has a separate "Show #<n>" button
 * that selects the target. An entry with `in_effect === false` is struck
 * through, with "Rewound by a later restore" as its title and as
 * screen-reader text.
 *
 * Detail (getTableChange): cached by id, the last DETAIL_CACHE_SIZE, under a
 * key of the table id, its `schema_version` and the newest restore entry
 * loaded -- a rows image is decoded and labelled with the table's CURRENT
 * schema, and entry fields (`in_effect`, `source_label`) move with restores,
 * so a schema change or a new restore drops the cache, and a response for an
 * older key is not cached. A rows change renders TableRowsDiffPreview over
 * `changeDetailToRowsDiff` (keys -> names, rows paired by `_id`; memoized
 * per detail); one whose images were cut to nothing (a row past the detail
 * byte cap) says it is too large to show, with its row count. A schema
 * change renders TableSchemaDiffPreview over `schema_diff`, a title change,
 * and the dropped-values note ("at least" once for the block when the values
 * were cut). Change #1 (`after.op === 'create_table'`; its `schema_diff`
 * diffs [] against the columns, every column "added") renders the created
 * columns through TableColumnsPreview, as the create_table card does. A
 * restore says "Restored the table to #<n>.", the number a button selecting
 * the target. A target outside the loaded pages can be selected too (the
 * detail carries the entry fields).
 *
 * Restore (only with `access.can_restore`): the confirm names how many
 * changes it rewinds (`restoreImpact`), whether the target was rewound (its
 * `in_effect` read from the current list entry, so a re-list while the
 * confirm is open updates it) and how to undo it (restore to the entry just
 * before the new restore entry: today's newest). The button is disabled,
 * with a note, when restoring would leave the table as it is
 * (`isCurrentState`: the selected change's state is the newest entry's,
 * following restore targets) -- the server would accept it and add a no-op
 * restore entry, which only clutters the log. On success: `onRestored(res)`
 * (the viewer applies `res.table`), the new restore entry goes on top of the
 * list at once and is selected, the list re-fetches, and focus moves to that
 * entry's list button (the heading when it has none): the button that
 * opened the confirm is disabled or gone by then.
 *
 * Keyboard: focus lands on the heading on open; Escape closes the drawer
 * while focus is inside it and no dialog is open (Escape in the confirm
 * closes only the confirm); Up / Down / Home / End move between list
 * entries.
 *
 * Phones (useIsMobile, as DocHistory): the drawer covers the screen and
 * shows the list alone; tapping an entry (or a "Show #n" target) opens its
 * detail full width with an "All changes" control back to the list, which
 * gets the focus. Escape steps back (detail -> list -> closed). Going back
 * focuses the selected entry. A restore keeps the detail open on the new
 * entry. TableHistory.css lays the phone panes out.
 */

import { useCallback, useEffect, useId, useMemo, useRef, useState, type KeyboardEvent, type ReactNode } from 'react';
import { Link } from 'react-router-dom';
import { ArrowLeft, ChevronLeft, RotateCcw, X } from 'lucide-react';
import { getTableChange, restoreTable } from '../../api/tablesApi';
import { ApiClientError } from '../../api/request';
import type {
  RestoreTableResponse,
  TableChange,
  TableChangeColumn,
  TableChangeDetail,
  TableChangeRowImage,
  TableDetail,
  TableRowsDiff,
  TableRowsDiffRow,
  TableSchemaDiffColumn,
  TableWriteConversation,
} from '../../api/types';
import { useIsMobile } from '../../hooks/useIsMobile';
import { isLostTableAccess, useTableChanges } from '../../hooks/useTableChanges';
import { formatRelativeTimestamp, parseUTCTimestamp } from '../../utils/formatters';
import { DocConfirmDialog } from '../docs/DocConfirmDialog';
import { TableColumnsPreview } from './TableColumnsPreview';
import { TableRowsDiffPreview } from './TableRowsDiffPreview';
import { TableSchemaDiffPreview } from './TableSchemaDiffPreview';
import './TableHistory.css';

export interface TableHistoryProps {
  table: TableDetail;
  /** Close the drawer. */
  onClose: () => void;
  /** A successful restore; the viewer applies `res.table` (useTable applyDetail). */
  onRestored: (res: RestoreTableResponse) => void;
}

const FORBIDDEN_NOTICE = 'You no longer have edit access to this table.';
const REWOUND_HINT = 'Rewound by a later restore';
/** Shown instead of a column key the table no longer has. */
export const REMOVED_COLUMN_LABEL = '(removed column)';

/** Change details kept in memory (most recently loaded). */
const DETAIL_CACHE_SIZE = 20;

const KIND_LABELS: Record<TableChange['kind'], string> = {
  rows: 'Rows',
  schema: 'Schema',
  restore: 'Restore',
};

// --- Pure helpers (exported for tests) -------------------------------------

/**
 * Display names by column key. A key the server labels with itself (`type`
 * null: no longer in the schema) reads REMOVED_COLUMN_LABEL; repeated names
 * get " 2", " 3", ... so every key has a distinct label (a diff keys values
 * by name).
 */
export function columnLabels(columns: TableChangeColumn[]): Map<string, string> {
  const labels = new Map<string, string>();
  const used = new Set<string>();
  for (const column of columns) {
    if (labels.has(column.key)) continue;
    const base = column.type === null || column.name === column.key ? REMOVED_COLUMN_LABEL : column.name;
    let name = base;
    for (let n = 2; used.has(name); n += 1) name = `${base} ${n}`;
    used.add(name);
    labels.set(column.key, name);
  }
  return labels;
}

/** Deep equality of two logical cell values (multi-enums are arrays). */
function sameValue(a: unknown, b: unknown): boolean {
  return JSON.stringify(a ?? null) === JSON.stringify(b ?? null);
}

function rowImages(value: TableChangeDetail['before']): TableChangeRowImage[] {
  return Array.isArray(value)
    ? value.filter((row): row is TableChangeRowImage => row !== null && typeof row === 'object' && typeof row._id === 'number')
    : [];
}

/**
 * A rows change's detail as a TableRowsDiff (values keyed by column NAME).
 *
 * What the images hold per op (chat/tables/storage.py): insert -> before
 * [], after the new rows; update -> both the patched rows; upsert -> before
 * only the rows that existed, after every written row; delete -> before the
 * removed rows, after []. So pairing by `_id` gives the status directly: in
 * after only -> added, in before only -> deleted, in both -> changed (with
 * the names of the cells that differ). Both images are sorted by `_id` and
 * cut independently; when one is truncated, a row past its last `_id` cannot
 * be paired, so rows past the shorter cut are left out (and `truncated` is
 * set). `_created_at` / `_updated_at` are dropped. Columns are the
 * `detail.columns` that occur in the images, in that order. `total` is the
 * number of rows the change touched.
 */
export function changeDetailToRowsDiff(detail: TableChangeDetail): TableRowsDiff {
  const before = rowImages(detail.before);
  const after = rowImages(detail.after);
  const labels = columnLabels(detail.columns);
  const present = new Set<string>();
  for (const row of [...before, ...after]) {
    for (const key of Object.keys(row)) if (!key.startsWith('_')) present.add(key);
  }
  const keys: string[] = detail.columns.map((c) => c.key).filter((k) => present.has(k));
  // Image keys the column list does not name (should not happen): keep them.
  for (const key of present) {
    if (!keys.includes(key)) {
      keys.push(key);
      if (!labels.has(key)) {
        const extra = columnLabels([
          ...detail.columns,
          { key, name: key, type: null, multiple: false, options: null, precision: null, current: false },
        ]);
        labels.set(key, extra.get(key) as string);
      }
    }
  }

  const named = (row: TableChangeRowImage): Record<string, unknown> => {
    const out: Record<string, unknown> = {};
    for (const key of keys) {
      if (key in row) out[labels.get(key) as string] = row[key];
    }
    return out;
  };

  const lastId = (rows: TableChangeRowImage[]) => (rows.length ? rows[rows.length - 1]._id : -Infinity);
  const cut = Math.min(
    detail.truncated?.before ? lastId(before) : Infinity,
    detail.truncated?.after ? lastId(after) : Infinity,
  );

  const beforeById = new Map(before.map((row) => [row._id, row]));
  const afterById = new Map(after.map((row) => [row._id, row]));
  const ids = Array.from(new Set([...beforeById.keys(), ...afterById.keys()]))
    .filter((id) => id <= cut)
    .sort((a, b) => a - b);

  const rows: TableRowsDiffRow[] = ids.map((id) => {
    const old = beforeById.get(id);
    const cur = afterById.get(id);
    if (old && cur) {
      const changed = keys
        .filter((key) => !sameValue(old[key], cur[key]))
        .map((key) => labels.get(key) as string);
      return { _id: id, status: 'changed', before: named(old), after: named(cur), changed_columns: changed };
    }
    if (cur) return { _id: id, status: 'added', before: null, after: named(cur), changed_columns: [] };
    return { _id: id, status: 'deleted', before: named(old as TableChangeRowImage), after: null, changed_columns: [] };
  });

  const total = Math.max(
    detail.row_ids_total ?? 0,
    detail.before_total ?? 0,
    detail.after_total ?? 0,
    rows.length,
  );
  return {
    columns: keys.map((key) => labels.get(key) as string),
    rows,
    total,
    truncated: Boolean(detail.truncated?.before || detail.truncated?.after) || rows.length < total,
  };
}

/** The change whose state `id` stands for: a restore's target, followed. */
function effectiveState(byId: Map<number, TableChange>, id: number): number {
  const seen = new Set<number>();
  let cur = id;
  for (;;) {
    const entry = byId.get(cur);
    if (!entry || entry.kind !== 'restore' || entry.restore_to === null || seen.has(cur)) return cur;
    seen.add(cur);
    cur = entry.restore_to;
  }
}

/**
 * True when restoring to `id` would leave the table as it is: its state is
 * the newest entry's (both resolved through restore targets on the loaded
 * entries; an unloaded target compares by id, so this errs towards false).
 */
export function isCurrentState(changes: TableChange[], id: number): boolean {
  if (changes.length === 0) return false;
  const byId = new Map(changes.map((c) => [c.id, c]));
  return effectiveState(byId, id) === effectiveState(byId, changes[0].id);
}

/**
 * The replay path of `id` (spec 4.5: `path(Q) = path(target) + [Q]` for a
 * restore, else `path(Q-1) + [Q]`) over the loaded entries: the loaded ids
 * on it, and `entry`, the first id it reaches that is not loaded (0 when it
 * runs down to the start of the log).
 */
function replayPath(byId: Map<number, TableChange>, id: number): { ids: Set<number>; entry: number } {
  const ids = new Set<number>();
  let cur = id;
  while (cur >= 1) {
    const entry = byId.get(cur);
    if (!entry || ids.has(cur)) return { ids, entry: entry ? 0 : cur };
    ids.add(cur);
    cur = entry.kind === 'restore' && entry.restore_to !== null && entry.restore_to < cur
      ? entry.restore_to
      : cur - 1;
  }
  return { ids, entry: 0 };
}

/**
 * What restoring to `id` rewinds: the data changes (restore entries only
 * point at a state, so they are not counted) on the newest entry's replay
 * path -- the entries in effect now -- that are not on `id`'s path. Usually
 * the in-effect changes newer than `id`, but a restore can have brought back
 * an older branch that `id` does not include. Exact when both paths leave
 * the loaded pages at the same id (below it they are the same); otherwise
 * the count covers the loaded entries only (`exact` false). `newestId` is
 * the entry a later restore would go back to in order to undo this one.
 */
export function restoreImpact(
  changes: TableChange[],
  id: number,
): { count: number; exact: boolean; newestId: number | null } {
  if (changes.length === 0) return { count: 0, exact: false, newestId: null };
  const byId = new Map(changes.map((c) => [c.id, c]));
  const now = replayPath(byId, changes[0].id);
  const target = replayPath(byId, id);
  let count = 0;
  for (const cid of now.ids) {
    if (!target.ids.has(cid) && byId.get(cid)?.kind !== 'restore') count += 1;
  }
  return { count, exact: now.entry === target.entry, newestId: changes[0].id };
}

/** The confirm's body sentences (exported for tests). */
export function restoreConfirmText(
  changes: TableChange[],
  target: { id: number; in_effect: boolean | null },
): string[] {
  const { count, exact, newestId } = restoreImpact(changes, target.id);
  const lines: string[] = [];
  if (count > 0) {
    const n = exact ? `${count}` : `at least ${count}`;
    lines.push(`This rewinds ${n} ${count === 1 ? 'change' : 'changes'}.`);
  }
  if (target.in_effect === false) {
    lines.push(`#${target.id} was rewound by a later restore; this brings it back.`);
  }
  lines.push(`The table goes back to how it was right after #${target.id}.`);
  if (newestId !== null) {
    lines.push(`You can undo this by restoring to #${newestId} afterwards.`);
  }
  return lines;
}

/** Dropped cells per column of a schema change: `[{name, count}]`. */
export function droppedValueCounts(detail: TableChangeDetail): { name: string; count: number }[] {
  const before = Array.isArray(detail.before) ? null : detail.before;
  const dropped = before?.dropped_values;
  if (!dropped || typeof dropped !== 'object') return [];
  const labels = columnLabels(detail.columns);
  return Object.entries(dropped as Record<string, unknown>)
    .map(([key, cells]) => ({
      name: labels.get(key) ?? REMOVED_COLUMN_LABEL,
      count: cells && typeof cells === 'object' ? Object.keys(cells).length : 0,
    }))
    .filter((entry) => entry.count > 0);
}

/**
 * Who to name next to `source_label`, or null when the label already names
 * them (a UI write is labelled with its actor; "you" for the viewer). The
 * owner (the only viewer for whom `owner_id` is themself) reads "you".
 */
export function actorLabel(change: TableChange, table: TableDetail): string | null {
  const actor = change.actor;
  if (!actor) return null;
  if (change.source?.startsWith('ui:')) return null;
  if (change.source_label === 'you' || change.source_label === actor.name) return null;
  const isViewer = !table.shared_with_me && actor.user_id === table.owner_id;
  // The viewer's own conversation: the link already says it was theirs.
  if (isViewer && change.conversation) return null;
  return isViewer ? 'you' : actor.name;
}

// --- Small pieces ----------------------------------------------------------

function conversationPath(conversation: TableWriteConversation): string {
  const id = encodeURIComponent(conversation.id);
  return conversation.project_id
    ? `/projects/${encodeURIComponent(conversation.project_id)}/${id}`
    : `/chats/${id}`;
}

function fullTimestamp(iso: string): string {
  return parseUTCTimestamp(iso).toLocaleString();
}

function rowsLabel(count: number): string {
  return `${count} ${count === 1 ? 'row' : 'rows'}`;
}

function errorMessage(err: unknown, fallback: string): string {
  return err instanceof ApiClientError && err.message ? err.message : fallback;
}

function isUnknownChange(err: unknown): boolean {
  return err instanceof ApiClientError && err.statusCode === 404 && err.errorCode === 'unknown_change';
}

/** "by <source_label>" (linked to its conversation when known) [+ actor]. */
function ChangeAuthor({ change, table }: { change: TableChange; table: TableDetail }) {
  const conversation = change.conversation;
  const actor = actorLabel(change, table);
  if (change.kind === 'restore') {
    return actor ? <>by {actor}</> : null;
  }
  return (
    <>
      by{' '}
      {conversation ? (
        <Link to={conversationPath(conversation)} className="table-history-link">
          {change.source_label || conversation.title || 'a conversation'}
        </Link>
      ) : (
        change.source_label
      )}
      {actor && <> ({actor})</>}
    </>
  );
}

function KindBadge({ kind }: { kind: TableChange['kind'] }) {
  return <span className={`table-history-badge table-history-badge--${kind}`}>{KIND_LABELS[kind]}</span>;
}

/**
 * Change #1's created columns, as TableColumnsPreview takes them: the stored
 * column dicts of `after.columns` (their `key` is only a React key there),
 * skipping anything that is not a named, typed column.
 */
export function createdColumns(detail: TableChangeDetail): TableSchemaDiffColumn[] {
  const after = Array.isArray(detail.after) ? null : detail.after;
  if (!after || !Array.isArray(after.columns)) return [];
  return (after.columns as unknown[]).filter(
    (column): column is TableSchemaDiffColumn =>
      column !== null &&
      typeof column === 'object' &&
      typeof (column as { name?: unknown }).name === 'string' &&
      typeof (column as { type?: unknown }).type === 'string',
  );
}

/** Change #1: the created columns, as the create_table card shows them. */
function CreatedColumns({ detail }: { detail: TableChangeDetail }) {
  const columns = createdColumns(detail);
  if (columns.length === 0) return <p className="table-history-muted">Created the table.</p>;
  return (
    <div>
      <p className="table-history-detail-caption">Created the table with these columns:</p>
      <TableColumnsPreview columns={columns} />
    </div>
  );
}

function SchemaDetail({ detail }: { detail: TableChangeDetail }) {
  const before = Array.isArray(detail.before) ? null : detail.before;
  const after = Array.isArray(detail.after) ? null : detail.after;
  if (after?.op === 'create_table') return <CreatedColumns detail={detail} />;
  const titleBefore = typeof before?.title === 'string' ? before.title : null;
  const titleAfter = typeof after?.title === 'string' ? after.title : null;
  const titleChanged = titleBefore !== null && titleAfter !== null && titleBefore !== titleAfter;
  const dropped = droppedValueCounts(detail);
  const columnsChanged = detail.schema_diff !== null && detail.schema_diff.changed;
  return (
    <div className="table-history-schema">
      {titleChanged && (
        <p className="table-history-detail-caption">
          Renamed the table from <strong>{titleBefore}</strong> to <strong>{titleAfter}</strong>.
        </p>
      )}
      {detail.schema_diff ? (
        (columnsChanged || !titleChanged) && <TableSchemaDiffPreview diff={detail.schema_diff} />
      ) : (
        <p className="table-history-muted">The column changes could not be read.</p>
      )}
      {dropped.length > 0 && (
        <p className="table-history-dropped">
          {/* One cut flag covers every column's values: "at least" once. */}
          {detail.truncated?.before ? 'Dropped values (at least): ' : 'Dropped values: '}
          {dropped.map((entry, index) => (
            <span key={entry.name}>
              {index > 0 && ', '}
              {entry.name}: {entry.count} {entry.count === 1 ? 'value' : 'values'}
            </span>
          ))}
          . Restoring to an earlier change brings them back.
        </p>
      )}
    </div>
  );
}

// --- Component -------------------------------------------------------------

/** A failed detail fetch, for the change it was for. */
interface DetailError {
  id: number;
  message: string;
  retryable: boolean;
}

/** Loaded details, valid for one cache key (see the header comment). */
interface DetailCache {
  key: string;
  details: TableChangeDetail[];
}

const NO_DETAILS: DetailCache = { key: '', details: [] };

/** The confirm's target: its id, and its `in_effect` when the confirm opened. */
type RestoreTarget = Pick<TableChange, 'id' | 'in_effect'>;

export function TableHistory({ table, onClose, onRestored }: TableHistoryProps) {
  const tableId = table.id;
  const idPrefix = useId();
  const isMobile = useIsMobile();
  const headingRef = useRef<HTMLHeadingElement>(null);
  const listRef = useRef<HTMLUListElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const allChangesRef = useRef<HTMLButtonElement>(null);
  // Phones: the detail shows (full width) instead of the list.
  const [phoneDetail, setPhoneDetail] = useState(false);
  // Where focus goes once the phone pane switch has rendered.
  const pendingFocusRef = useRef<'detail' | 'list' | null>(null);
  // The table shown now, for a restore that settles after a switch.
  const tableIdRef = useRef(tableId);
  tableIdRef.current = tableId;

  // Set by a lost-access answer from a detail / restore call; sticky while open.
  const [forbidden, setForbidden] = useState(false);
  const canEdit = table.access.can_edit;
  const list = useTableChanges(tableId, { enabled: canEdit && !forbidden });
  const lostAccess = forbidden || list.forbidden || !canEdit;
  const { changes, hasMore, refresh, addChange } = list;

  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [detailCache, setDetailCache] = useState<DetailCache>(NO_DETAILS);
  const [detailError, setDetailError] = useState<DetailError | null>(null);
  const [detailRetry, setDetailRetry] = useState(0);
  const detailSeqRef = useRef(0);
  const [restoreTarget, setRestoreTarget] = useState<RestoreTarget | null>(null);
  const [restoreBusy, setRestoreBusy] = useState(false);
  const [restoreError, setRestoreError] = useState<string | null>(null);
  // A restore just landed: focus its entry once it is on the page.
  const [focusAfterRestore, setFocusAfterRestore] = useState<{ id: number | null } | null>(null);

  // A new table resets the drawer (the parent remounts it per table; this
  // is a cheap guard).
  const [shownTableId, setShownTableId] = useState(tableId);
  if (shownTableId !== tableId) {
    setShownTableId(tableId);
    setSelectedId(null);
    setDetailCache(NO_DETAILS);
    setDetailError(null);
    setForbidden(false);
    setRestoreTarget(null);
    setRestoreBusy(false);
    setRestoreError(null);
    setFocusAfterRestore(null);
    setPhoneDetail(false);
  }

  // --- Detail --------------------------------------------------------------
  const newestRestoreId = useMemo(
    () => changes.reduce((max, c) => (c.kind === 'restore' && c.id > max ? c.id : max), 0),
    [changes],
  );
  const cacheKey = `${tableId}:${table.schema_version}:${newestRestoreId}`;
  const cacheKeyRef = useRef(cacheKey);
  cacheKeyRef.current = cacheKey;
  const details = detailCache.key === cacheKey ? detailCache.details : NO_DETAILS.details;

  const detail = selectedId === null ? undefined : details.find((d) => d.id === selectedId);
  const needDetail = selectedId !== null && detail === undefined;
  const rowsDiff = useMemo(
    () => (detail?.kind === 'rows' ? changeDetailToRowsDiff(detail) : null),
    [detail],
  );

  useEffect(() => {
    const seq = ++detailSeqRef.current;
    if (lostAccess || selectedId === null || !needDetail) return;
    const id = selectedId;
    const key = cacheKey;
    setDetailError(null);
    getTableChange(tableId, id).then(
      (loaded) => {
        // Cached whatever is selected by the time it lands, unless the
        // cache key moved on meanwhile (decoded with an older schema).
        setDetailCache((prev) => {
          if (key !== cacheKeyRef.current) return prev;
          const kept = prev.key === key ? prev.details.filter((d) => d.id !== id) : [];
          return { key, details: [...kept, loaded].slice(-DETAIL_CACHE_SIZE) };
        });
      },
      (err: unknown) => {
        if (seq !== detailSeqRef.current) return;
        if (isLostTableAccess(err)) setForbidden(true);
        else if (isUnknownChange(err)) {
          setDetailError({ id, message: errorMessage(err, `There is no change #${id}.`), retryable: false });
        } else {
          setDetailError({ id, message: errorMessage(err, 'Failed to load this change.'), retryable: true });
        }
      },
    );
  }, [tableId, selectedId, needDetail, detailRetry, lostAccess, cacheKey]);

  // The entry as listed (current `in_effect`), else the detail's own fields.
  const selectedEntry: TableChange | null =
    selectedId === null ? null : (changes.find((c) => c.id === selectedId) ?? detail ?? null);

  /** Select a change; on phones its detail replaces the list. */
  const select = useCallback(
    (id: number) => {
      setSelectedId(id);
      if (isMobile && !phoneDetail) {
        pendingFocusRef.current = 'detail';
        setPhoneDetail(true);
      }
    },
    [isMobile, phoneDetail],
  );

  /** Select a change and bring its list entry into view when loaded (phones: open its detail). */
  const jumpTo = useCallback(
    (id: number) => {
      if (isMobile) {
        select(id);
        return;
      }
      setSelectedId(id);
      setTimeout(() => {
        const button = listRef.current?.querySelector<HTMLButtonElement>(`[data-change-id="${id}"]`);
        if (button) {
          button.scrollIntoView?.({ block: 'nearest' });
          button.focus();
        }
      }, 0);
    },
    [isMobile, select],
  );

  const backToList = useCallback(() => {
    pendingFocusRef.current = 'list';
    setPhoneDetail(false);
  }, []);

  // --- Restore -------------------------------------------------------------
  const restoreOpen = restoreTarget !== null && !lostAccess;
  const confirmLines = useMemo(() => {
    if (restoreTarget === null) return [];
    // The listed entry's `in_effect` (a re-list may have moved it since).
    const listed = changes.find((c) => c.id === restoreTarget.id);
    return restoreConfirmText(changes, listed ?? restoreTarget);
  }, [restoreTarget, changes]);

  const closeRestore = useCallback(() => {
    setRestoreTarget(null);
    setRestoreError(null);
  }, []);

  const confirmRestore = useCallback(async () => {
    const target = restoreTarget;
    if (target === null || restoreBusy) return;
    const forTable = tableId;
    setRestoreBusy(true);
    setRestoreError(null);
    try {
      const res = await restoreTable(forTable, target.id);
      if (tableIdRef.current !== forTable) return;
      setRestoreBusy(false);
      setRestoreTarget(null);
      onRestored(res);
      const made = res.change;
      // On top at once, so the new entry is the newest (its Restore button
      // disabled) before the re-list lands.
      if (made) addChange(made);
      setSelectedId(made ? made.id : null);
      setFocusAfterRestore({ id: made ? made.id : null });
      void refresh();
    } catch (err) {
      if (tableIdRef.current !== forTable) return;
      setRestoreBusy(false);
      if (isLostTableAccess(err)) setForbidden(true);
      else setRestoreError(errorMessage(err, 'Failed to restore the table.'));
    }
  }, [tableId, restoreTarget, restoreBusy, onRestored, addChange, refresh]);

  // Lost access drops any open dialog for good.
  useEffect(() => {
    if (!lostAccess) return;
    setRestoreTarget(null);
    setRestoreError(null);
  }, [lostAccess]);

  // --- Focus and keyboard --------------------------------------------------
  useEffect(() => {
    headingRef.current?.focus();
  }, []);

  useEffect(() => {
    if (lostAccess) closeRef.current?.focus();
  }, [lostAccess]);

  // After a restore the button that opened the confirm is disabled or gone
  // (focus would fall to the page, where Escape no longer reaches the
  // drawer): move it to the new entry's list button, else (the phone
  // detail) "All changes", else the heading. Runs after the confirm's own
  // focus return (passive cleanups run first).
  useEffect(() => {
    if (focusAfterRestore === null) return;
    setFocusAfterRestore(null);
    const { id } = focusAfterRestore;
    const button =
      id === null ? null : listRef.current?.querySelector<HTMLButtonElement>(`[data-change-id="${id}"]`);
    if (button) {
      button.scrollIntoView?.({ block: 'nearest' });
      button.focus();
    } else if (allChangesRef.current) {
      allChangesRef.current.focus();
    } else {
      headingRef.current?.focus();
    }
  }, [focusAfterRestore]);

  // The phone pane switch landed: focus "All changes" (detail) or the
  // selected entry (list), so focus never falls to the page.
  useEffect(() => {
    const target = pendingFocusRef.current;
    if (target === null) return;
    pendingFocusRef.current = null;
    if (target === 'detail') allChangesRef.current?.focus();
    else {
      const button = listRef.current?.querySelector<HTMLButtonElement>('.table-history-item-select[aria-current="true"]');
      button?.scrollIntoView?.({ block: 'nearest' });
      button?.focus();
    }
  }, [phoneDetail]);

  // A phone shows the detail only for a selection (a restore without an
  // entry clears it: back to the list).
  const phoneShowsDetail = isMobile && phoneDetail && selectedId !== null;
  const phoneDetailShown = phoneShowsDetail && !lostAccess && !list.loading && list.error === null;

  const handleRootKeyDown = (e: KeyboardEvent<HTMLElement>) => {
    // The portaled dialog's key events bubble through here; its own Escape
    // closes it, not the drawer.
    if (e.key !== 'Escape' || e.defaultPrevented || restoreOpen) return;
    e.preventDefault();
    if (phoneDetailShown) backToList();
    else onClose();
  };

  const handleListKeyDown = (e: KeyboardEvent<HTMLUListElement>) => {
    if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(e.key)) return;
    const buttons = Array.from(e.currentTarget.querySelectorAll<HTMLButtonElement>('.table-history-item-select'));
    const index = buttons.indexOf(document.activeElement as HTMLButtonElement);
    if (index === -1) return;
    e.preventDefault();
    let next = index;
    if (e.key === 'ArrowDown') next = Math.min(buttons.length - 1, index + 1);
    else if (e.key === 'ArrowUp') next = Math.max(0, index - 1);
    else if (e.key === 'Home') next = 0;
    else next = buttons.length - 1;
    buttons[next].focus();
  };

  // --- Render --------------------------------------------------------------
  const renderEntry = (change: TableChange) => {
    const selected = change.id === selectedId;
    const rewound = change.in_effect === false;
    const metaId = `${idPrefix}-meta-${change.id}`;
    return (
      <li
        key={change.id}
        className={`table-history-item${selected ? ' is-selected' : ''}${rewound ? ' is-rewound' : ''}`}
        title={rewound ? REWOUND_HINT : undefined}
      >
        <button
          type="button"
          className="table-history-item-select"
          data-change-id={change.id}
          aria-current={selected ? 'true' : undefined}
          aria-describedby={metaId}
          onClick={() => select(change.id)}
        >
          <span className="table-history-item-id">#{change.id}</span>
          <KindBadge kind={change.kind} />
          <span className="table-history-item-summary">
            {change.kind === 'restore' && change.restore_to !== null
              ? `Restored to #${change.restore_to}`
              : change.summary}
          </span>
          {rewound && <span className="table-history-sr-only"> ({REWOUND_HINT})</span>}
        </button>
        <div id={metaId} className="table-history-item-meta">
          <time dateTime={change.created_at} title={fullTimestamp(change.created_at)}>
            {formatRelativeTimestamp(change.created_at)}
          </time>{' '}
          <ChangeAuthor change={change} table={table} />
          {change.kind === 'rows' && change.row_count !== null && (
            <>
              <span className="table-history-sep" aria-hidden="true"> · </span>
              {rowsLabel(change.row_count)}
            </>
          )}
          {change.kind === 'restore' && change.restore_to !== null && (
            <>
              <span className="table-history-sep" aria-hidden="true"> · </span>
              <button
                type="button"
                className="table-history-target"
                onClick={() => jumpTo(change.restore_to as number)}
              >
                Show #{change.restore_to}
              </button>
            </>
          )}
        </div>
      </li>
    );
  };

  const renderDetail = (): ReactNode => {
    if (selectedId === null) {
      return <p className="table-history-muted table-history-placeholder">Select a change to see what it did.</p>;
    }
    const error = detailError?.id === selectedId ? detailError : null;
    const entry = selectedEntry;
    const canRestore = table.access.can_restore && entry !== null && !(error && !error.retryable);
    const current = entry !== null && isCurrentState(changes, entry.id);

    let body: ReactNode;
    if (detail) {
      if (rowsDiff) {
        if (rowsDiff.rows.length > 0) body = <TableRowsDiffPreview diff={rowsDiff} />;
        else if (rowsDiff.truncated) {
          // The images were cut to nothing (one row past the detail byte cap).
          body = (
            <p className="table-history-muted">
              This change is too large to show here ({rowsLabel(rowsDiff.total)}).
            </p>
          );
        } else body = <p className="table-history-muted">No row images were recorded for this change.</p>;
      } else if (detail.kind === 'schema') {
        body = <SchemaDetail detail={detail} />;
      } else {
        const to = detail.restore_to;
        body = (
          <p className="table-history-detail-caption">
            {to !== null ? (
              <>
                Restored the table to{' '}
                <button type="button" className="table-history-target" onClick={() => jumpTo(to)}>
                  #{to}
                </button>
                .
              </>
            ) : (
              'Restored the table.'
            )}
          </p>
        );
      }
    } else if (error) {
      body = (
        <div className="table-history-detail-state">
          <p className="table-history-state-text">{error.message}</p>
          {error.retryable && (
            <button type="button" className="table-history-button" onClick={() => setDetailRetry((n) => n + 1)}>
              Retry
            </button>
          )}
        </div>
      );
    } else {
      body = <p className="table-history-muted">Loading this change...</p>;
    }

    return (
      <article className="table-history-detail" aria-label={`Change #${selectedId}`}>
        {isMobile && (
          <button
            ref={allChangesRef}
            type="button"
            className="table-history-button table-history-all-changes"
            onClick={backToList}
          >
            <ChevronLeft size={16} aria-hidden="true" />
            All changes
          </button>
        )}
        <div className="table-history-detail-head">
          <div className="table-history-detail-heading">
            <h3 className={`table-history-detail-title${entry?.in_effect === false ? ' is-rewound' : ''}`}>
              #{selectedId}
              {entry && <> {entry.kind === 'restore' && entry.restore_to !== null ? `Restored to #${entry.restore_to}` : entry.summary}</>}
            </h3>
            {entry && (
              <p className="table-history-detail-meta">
                <time dateTime={entry.created_at} title={fullTimestamp(entry.created_at)}>
                  {formatRelativeTimestamp(entry.created_at)}
                </time>{' '}
                <ChangeAuthor change={entry} table={table} />
                {entry.in_effect === false && (
                  <>
                    <span className="table-history-sep" aria-hidden="true"> · </span>
                    {REWOUND_HINT}
                  </>
                )}
              </p>
            )}
          </div>
          {canRestore && entry && (
            <div className="table-history-actions">
              <button
                type="button"
                className="table-history-button table-history-button--primary"
                disabled={current}
                aria-describedby={current ? `${idPrefix}-current-note` : undefined}
                onClick={() => {
                  setRestoreError(null);
                  setRestoreTarget({ id: entry.id, in_effect: entry.in_effect });
                }}
              >
                <RotateCcw size={15} aria-hidden="true" />
                Restore table to this point
              </button>
              {current && (
                <span id={`${idPrefix}-current-note`} className="table-history-note">
                  The table is already in this state.
                </span>
              )}
            </div>
          )}
        </div>
        <div className="table-history-detail-body">{body}</div>
      </article>
    );
  };

  let content: ReactNode;
  if (lostAccess) {
    content = (
      <div className="table-history-state" role="alert">
        <p className="table-history-state-text">{FORBIDDEN_NOTICE}</p>
        <button type="button" className="table-history-button" onClick={onClose}>
          <ArrowLeft size={15} aria-hidden="true" />
          Back to table
        </button>
      </div>
    );
  } else if (list.loading) {
    content = <p className="table-history-state">Loading history...</p>;
  } else if (list.error !== null) {
    content = (
      <div className="table-history-state">
        <p className="table-history-state-text">{list.error}</p>
        <button type="button" className="table-history-button" onClick={() => void refresh()}>
          Retry
        </button>
      </div>
    );
  } else {
    const showList = !phoneShowsDetail;
    const showDetail = !isMobile || phoneShowsDetail;
    content = (
      <div className={`table-history-body${isMobile ? ' table-history-body--phone' : ''}`}>
        {showList && (
          <section className="table-history-list-pane" aria-label="Changes">
            {changes.length === 0 ? (
              <p className="table-history-muted">No changes yet.</p>
            ) : (
              <ul ref={listRef} className="table-history-list" onKeyDown={handleListKeyDown}>
                {changes.map(renderEntry)}
              </ul>
            )}
            {hasMore && (
              <button
                type="button"
                className="table-history-button table-history-more"
                disabled={list.loadingMore}
                onClick={list.loadMore}
              >
                {list.loadingMore ? 'Loading...' : 'Load more'}
              </button>
            )}
            {list.loadMoreError && (
              <p className="table-history-error" role="alert">
                {list.loadMoreError}
              </p>
            )}
          </section>
        )}
        {showDetail && renderDetail()}
      </div>
    );
  }

  return (
    <aside className="table-history" aria-labelledby={`${idPrefix}-title`} onKeyDown={handleRootKeyDown}>
      <div className="table-history-header">
        <h2 id={`${idPrefix}-title`} ref={headingRef} tabIndex={-1} className="table-history-title">
          History
        </h2>
        <button
          ref={closeRef}
          type="button"
          className="table-history-close"
          aria-label="Close history"
          onClick={onClose}
        >
          <X size={16} aria-hidden="true" />
        </button>
      </div>
      {content}
      <DocConfirmDialog
        isOpen={restoreOpen}
        title="Restore the table?"
        confirmLabel="Restore"
        busyLabel="Restoring..."
        busy={restoreBusy}
        error={restoreError}
        onConfirm={() => void confirmRestore()}
        onClose={closeRestore}
      >
        {confirmLines.map((line) => (
          <p key={line}>{line}</p>
        ))}
      </DocConfirmDialog>
    </aside>
  );
}
