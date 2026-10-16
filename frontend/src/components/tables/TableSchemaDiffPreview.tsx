/**
 * A table schema diff as a compact before / after list. Shared by the
 * change_table_schema approval card (preview field type `table_schema_diff`,
 * payload `field.schema_diff`: key-less, with `cells_rewritten`,
 * `options_renamed` and `cells_rewritten_total`) and the History detail of a
 * schema change (entries carry `key`, no rewrite counts). Both render the
 * same; a column key is only ever a React key, never text.
 *
 * - One row per entry in the server's order (chat/tables/schema_ops.py
 *   `schema_diff()`): the after order, each dropped column right after its
 *   nearest surviving predecessor in the before order (first when it has
 *   none), so a drop shows where the column was. No reordering here.
 * - Columns: a status badge (added / dropped / renamed / changed /
 *   unchanged, plus "moved" when `moved`; a column that only moved shows
 *   just "moved"), the before side and the after side (name,
 *   `columnTypeLabel()`, required / unique flags; a dash for the side an
 *   added or dropped column lacks). The header cells carry `scope="col"`,
 *   the empty badge header a visually hidden "Status".
 * - Under a row: the field changes as lines (`schemaChangeLines()`: "Type:
 *   text → enum", "Required: no → yes", "Default: Open → Done", "Precision:
 *   2 → 0", "Description changed"; name and options are left to the sides
 *   and chips; values cut by `previewText()`), the option chips
 *   (`schemaOptionChips()`: renames from `options_renamed` as "Done →
 *   Closed", the rest "+ Closed" / "- Done"), and "N cells rewritten".
 * - Unchanged, unmoved rows are muted; more than UNCHANGED_COLLAPSE_AT of
 *   them fold behind a "Show N unchanged columns" toggle.
 * - Footer: "Column order changed: A, B, C" when `order_changed`, "No
 *   column changes" when `changed` is false (e.g. a table rename only). The
 *   card's total of rewritten cells is its own "Cells rewritten" preview
 *   field, so it is not repeated here.
 *
 * Colours: the badge classes `table-diff-badge status-<status>` use the
 * shared `--diff-*` tokens of src/index.css (added green, dropped red,
 * renamed / changed / moved amber).
 */
import { useState } from 'react';
import type { TableSchemaDiff, TableSchemaDiffColumn, TableSchemaDiffEntry } from '../../api/types';
import { columnTypeLabel, previewText, tableValueText } from './TableColumnsPreview';
import './TableColumnsPreview.css';
import './TableSchemaDiffPreview.css';

/** Unchanged, unmoved rows beyond this count fold behind a toggle. */
export const UNCHANGED_COLLAPSE_AT = 3;

/** Visually hidden label of the empty header cell over the badges. */
export const STATUS_HEADER_LABEL = 'Status';

const ARROW = '→';

const FIELD_ORDER = ['type', 'multiple', 'precision', 'required', 'unique', 'default', 'description'];
// Shown by the before / after sides and the option chips instead.
const FIELDS_NOT_LISTED = new Set(['name', 'options', 'key']);
const FIELD_LABELS: Record<string, string> = {
  type: 'Type',
  multiple: 'Multiple',
  precision: 'Precision',
  required: 'Required',
  unique: 'Unique',
  default: 'Default',
};

function fieldValueText(field: string, value: unknown): string {
  if (field === 'required' || field === 'unique' || field === 'multiple') {
    return value ? 'yes' : 'no';
  }
  const text = tableValueText(value);
  if (text === null) return 'none';
  if (text === '') return '""';
  return previewText(text);
}

function capitalize(text: string): string {
  return text.charAt(0).toUpperCase() + text.slice(1);
}

/**
 * The field changes of one entry as readable lines, in a fixed field order
 * (unknown fields last, alphabetically). The name and the options are not
 * listed: the before / after sides and the option chips show them.
 */
export function schemaChangeLines(entry: Pick<TableSchemaDiffEntry, 'changes'>): string[] {
  const fields = Object.keys(entry.changes ?? {}).filter((f) => !FIELDS_NOT_LISTED.has(f));
  fields.sort((a, b) => {
    const ia = FIELD_ORDER.indexOf(a);
    const ib = FIELD_ORDER.indexOf(b);
    if (ia !== -1 || ib !== -1) {
      return (ia === -1 ? FIELD_ORDER.length : ia) - (ib === -1 ? FIELD_ORDER.length : ib);
    }
    return a.localeCompare(b);
  });
  return fields.map((field) => {
    const { before, after } = entry.changes[field];
    if (field === 'description') {
      if (!before) return 'Description added';
      if (!after) return 'Description removed';
      return 'Description changed';
    }
    const label = FIELD_LABELS[field];
    if (!label) return `${capitalize(field)} changed`;
    return `${label}: ${fieldValueText(field, before)} ${ARROW} ${fieldValueText(field, after)}`;
  });
}

export interface SchemaOptionChip {
  kind: 'renamed' | 'added' | 'removed';
  label: string;
}

/**
 * What schemaOptionChips reads: an entry's option lists and, optionally,
 * its sides' options (without them the +/- lists stand in).
 */
export type SchemaOptionChipsInput =
  Pick<TableSchemaDiffEntry, 'options_added' | 'options_removed' | 'options_renamed'>
  & {
    before?: Pick<TableSchemaDiffColumn, 'options'> | null;
    after?: Pick<TableSchemaDiffColumn, 'options'> | null;
  };

/**
 * The enum option changes of one entry as chips: renames first, then
 * "+ <added>", then "- <removed>".
 *
 * Renames come from `options_renamed` (in order), tracked as [origin,
 * current] pairs: a rename of a live current name moves that pair on (a
 * chain A → B → C nets to A → C, a round trip drops out); any other rename
 * starts a pair whose origin counts as an original option only when the
 * column had it before and no earlier pair already renamed it away (so
 * rename A → B, add A, rename A → C reads "A → B, + C"). A pair is shown
 * when its origin is original and its current name exists after; its names
 * then leave the +/- chips. An original option renamed away but present
 * again after (re-added) gets its own "+" chip next to the rename; an
 * original option whose name a rename took over without being renamed
 * itself gets a "-" chip.
 *
 * "Had before" / "exists after" read the `before` / `after` sides' options;
 * without the sides (or with a side missing, as for an added column) they
 * fall back to `options_removed` / `options_added`, which shows a rename
 * only when its origin was removed and its target added.
 */
export function schemaOptionChips(entry: SchemaOptionChipsInput): SchemaOptionChip[] {
  const added = entry.options_added ?? [];
  const removed = entry.options_removed ?? [];
  const beforeOptions = entry.before ? (entry.before.options ?? []) : null;
  const afterOptions = entry.after ? (entry.after.options ?? []) : null;
  const hadBefore = (option: string) => (beforeOptions ?? removed).includes(option);
  const existsAfter = (option: string) => (afterOptions ?? added).includes(option);

  const pairs: { origin: string; current: string; original: boolean }[] = [];
  for (const { option, new_option: newOption } of entry.options_renamed ?? []) {
    let live: (typeof pairs)[number] | undefined;
    for (let i = pairs.length - 1; i >= 0 && !live; i -= 1) {
      if (pairs[i].current === option) live = pairs[i];
    }
    if (live) {
      live.current = newOption;
    } else {
      const original = hadBefore(option) && !pairs.some((p) => p.original && p.origin === option);
      pairs.push({ origin: option, current: newOption, original });
    }
  }
  const renames = pairs.filter((p) => p.original && p.origin !== p.current && existsAfter(p.current));
  const renamedFrom = new Set(renames.map((p) => p.origin));
  const renamedTo = new Set(renames.map((p) => p.current));

  const chips: SchemaOptionChip[] = renames.map((p) => ({
    kind: 'renamed',
    label: `${p.origin} ${ARROW} ${p.current}`,
  }));
  for (const option of added) {
    if (!renamedTo.has(option)) chips.push({ kind: 'added', label: `+ ${option}` });
  }
  for (const option of renamedFrom) {
    if (!renamedTo.has(option) && existsAfter(option) && !added.includes(option)) {
      chips.push({ kind: 'added', label: `+ ${option}` });
    }
  }
  for (const option of removed) {
    if (!renamedFrom.has(option)) chips.push({ kind: 'removed', label: `- ${option}` });
  }
  for (const option of renamedTo) {
    if (!renamedFrom.has(option) && hadBefore(option) && !removed.includes(option)) {
      chips.push({ kind: 'removed', label: `- ${option}` });
    }
  }
  return chips;
}

function plural(count: number, noun: string): string {
  return `${count} ${noun}${count === 1 ? '' : 's'}`;
}

function ColumnSide({ column }: { column: TableSchemaDiffColumn | null }) {
  if (!column) return <span className="table-schema-diff-none">—</span>;
  return (
    <span className="table-schema-diff-col">
      <span className="table-schema-diff-name">{column.name}</span>
      <span className="table-schema-diff-type">{columnTypeLabel(column)}</span>
      {column.required && <span className="table-schema-diff-flag">required</span>}
      {column.unique && <span className="table-schema-diff-flag">unique</span>}
    </span>
  );
}

function entryKey(entry: TableSchemaDiffEntry, index: number): string {
  return entry.key ?? `${index}:${entry.before?.name ?? ''}:${entry.after?.name ?? ''}`;
}

export interface TableSchemaDiffPreviewProps {
  diff: TableSchemaDiff;
}

export function TableSchemaDiffPreview({ diff }: TableSchemaDiffPreviewProps) {
  const [showUnchanged, setShowUnchanged] = useState(false);
  const entries = diff.columns ?? [];
  const isQuiet = (e: TableSchemaDiffEntry) => e.status === 'unchanged' && !e.moved;
  const quietCount = entries.filter(isQuiet).length;
  const collapsible = quietCount > UNCHANGED_COLLAPSE_AT;
  const visible = entries
    .map((entry, index) => ({ entry, index }))
    .filter(({ entry }) => !collapsible || showUnchanged || !isQuiet(entry));

  return (
    <div className="table-schema-diff">
      {visible.length > 0 && (
        <table className="table-schema-diff-table">
          <thead>
            <tr>
              <th scope="col">
                <span className="table-schema-diff-sr-only">{STATUS_HEADER_LABEL}</span>
              </th>
              <th scope="col">Before</th>
              <th scope="col">After</th>
            </tr>
          </thead>
          {visible.map(({ entry, index }) => {
            const lines = schemaChangeLines(entry);
            const chips = schemaOptionChips(entry);
            const rewritten = entry.cells_rewritten ?? 0;
            const hasDetails = lines.length > 0 || chips.length > 0 || rewritten > 0;
            return (
              <tbody
                key={entryKey(entry, index)}
                className={`table-schema-diff-entry status-${entry.status}${isQuiet(entry) ? ' quiet' : ''}`}
              >
                <tr>
                  <td className="table-schema-diff-badges">
                    {!(entry.moved && entry.status === 'unchanged') && (
                      <span className={`table-diff-badge status-${entry.status}`}>{entry.status}</span>
                    )}
                    {entry.moved && <span className="table-diff-badge status-moved">moved</span>}
                  </td>
                  <td className="table-schema-diff-side"><ColumnSide column={entry.before} /></td>
                  <td className="table-schema-diff-side"><ColumnSide column={entry.after} /></td>
                </tr>
                {hasDetails && (
                  <tr className="table-schema-diff-details">
                    <td />
                    <td colSpan={2}>
                      {lines.length > 0 && (
                        <ul className="table-schema-diff-lines">
                          {lines.map((line) => <li key={line}>{line}</li>)}
                        </ul>
                      )}
                      {chips.length > 0 && (
                        <span className="table-diff-chips">
                          {chips.map((chip) => (
                            <span key={chip.label} className={`table-diff-chip ${chip.kind}`}>
                              {chip.label}
                            </span>
                          ))}
                        </span>
                      )}
                      {rewritten > 0 && (
                        <div className="table-schema-diff-rewritten">
                          {plural(rewritten, 'cell')} rewritten
                        </div>
                      )}
                    </td>
                  </tr>
                )}
              </tbody>
            );
          })}
        </table>
      )}
      {(collapsible || !diff.changed || diff.order_changed) && (
        <div className="table-schema-diff-footer">
          {collapsible && (
            <button
              type="button"
              className="table-schema-diff-toggle"
              onClick={() => setShowUnchanged((v) => !v)}
              aria-expanded={showUnchanged}
            >
              {showUnchanged ? 'Hide unchanged columns' : `Show ${plural(quietCount, 'unchanged column')}`}
            </button>
          )}
          {!diff.changed && <span className="table-schema-diff-note">No column changes</span>}
          {diff.order_changed && (
            <span className="table-schema-diff-note">
              Column order changed: {(diff.after_order ?? []).join(', ')}
            </span>
          )}
        </div>
      )}
    </div>
  );
}
