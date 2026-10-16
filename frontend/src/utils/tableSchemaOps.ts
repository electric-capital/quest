/**
 * Quest Tables schema editor logic (pure): the editable draft of a table's
 * columns, its validation against the server's rules, and the derivation of
 * the schema operations (spec 4.4) that turn the loaded schema into the
 * draft as ONE `PUT /tables/{id}/schema` (one change, one restore point).
 * The New Table dialog uses the same draft and turns it into a
 * `POST /tables` body instead (`draftToCreateRequest`).
 *
 * Draft model. A `DraftColumn` keeps the column's KEY (null for a column
 * the draft adds) next to a stable React `id` (the key itself, or a temp id),
 * so a rename is a rename and never a drop plus an add. Each enum option is
 * a `DraftOption` with the option `original`ly loaded (null when added), so
 * a renamed option is `rename_enum_option`, not remove + add. An enum
 * default is held as option ids (`defaultOptionIds`), so renaming an option
 * keeps it as the default; every other default is the text of its input
 * (`defaultValue`: '' = none; booleans 'true' / 'false'). Precision is the
 * text of its input too ('' = none), so a bad entry can be shown at the
 * field.
 *
 * Validation (`validateDraft`) mirrors chat/tables/schema.py and
 * schema_ops.py where that is cheap -- names (`validate_column_name`,
 * `name_taken`), options (`_check_options`), description
 * (`validate_column_description`), title (`validate_table_title`), the
 * precision / unique / multiple field rules (`_build_column`), defaults
 * (`coerce_value`: number grammar and 38 digits, http(s) URL with a host,
 * text length / NUL, options that exist), `required_needs_default`
 * (`_add_column` on a table with rows), `multi_to_single`
 * (`_set_enum_multiple`), `last_column` / `last_option`, `default_uses_option`
 * and the fixed type (`type_change_not_supported`) -- and leaves the rest to
 * the server (rows that break required / unique / a removed option come
 * back as `rows_violate`).
 *
 * Derivation (`deriveSchemaChange`). The server applies operations in order
 * against a working copy and addresses columns (and options) by their name
 * AT THAT POINT of the list (chat/tables/schema_ops.py `_Batch.column`,
 * `find_column`: exact match; collisions via `name_taken`: case-insensitive),
 * so this module simulates the same working copy. The order:
 *   1. `rename_table` (the title changed);
 *   2. `drop_column` for every loaded column the draft no longer has, by its
 *      loaded name (first, so their names are free for renames and adds).
 *      When the draft keeps NO loaded column, one drop waits until the first
 *      new column is in (the server refuses to drop the only column,
 *      `last_column`); if that new column takes its name, it is first
 *      renamed to a temporary name;
 *   3. `rename_column`s, each only once its target name is free (compared
 *      case-insensitively): a chain (A -> B while B -> C) runs back to front,
 *      a cycle (A -> B while B -> A) parks one column under a temporary name;
 *   4. per surviving column, in draft order, addressed by its NEW name:
 *      `set_description`, `set_required`, `set_unique`, `set_precision`
 *      (before the default: the server re-rounds the default to the new
 *      precision), `set_enum_multiple` (single -> multi; the server wraps the
 *      default into a list), then the options: when the current default uses
 *      an option about to be removed (`default_uses_option`), a `set_default`
 *      first -- to the wanted default when it only uses kept options (by
 *      their loaded spelling), else to null; `remove_enum_option`s (before
 *      renames and adds, so a freed spelling can be reused; when no loaded
 *      option stays, one removal waits until the first new option is in,
 *      `last_option`); `rename_enum_option`s (same chain / cycle care; the
 *      server carries the default along); `add_enum_option`s with `after` =
 *      the option before it in the draft; and finally `set_default` when the
 *      default differs from what the steps above left (after the adds, so a
 *      new option can be the default);
 *   5. `add_column` for each new column in draft order with `after` = the
 *      column before it in the draft (by its final name), appended when it
 *      is first;
 *   6. `reorder_columns` with every FINAL name, last, only when the order
 *      the steps above produce differs from the draft's.
 * No operation is emitted for an unchanged field (a number default compares
 * as a decimal, an enum multi default as a set in option order). The server
 * has no operation that reorders the options of an existing column, and
 * `add_enum_option` without `after` appends, so the editor never moves an
 * existing option and appends new ones (a new COLUMN's options are free:
 * they ride in its `add_column` spec). A new option that spells (ignoring
 * case) an option the draft removed counts as that option kept -- renamed
 * when the case differs -- where it sits in the loaded option's place, so
 * removing an option and typing it again never becomes a removal the
 * server refuses while rows use it (`settleReaddedOptions` moves such an
 * option into place for the editor). At most TABLE_SCHEMA_MAX_OPERATIONS
 * operations fit one save; the editor checks the count.
 *
 * The editor counts a change per edited thing, not per operation
 * (`summarizeSchemaChange`, `schemaChangePhrase`: "2 columns changed").
 * Names and options compare case-insensitively through `foldName`, close to
 * the server's `casefold` ('ß' matches 'SS').
 */

import type {
  CreateTableRequest,
  TableCellValue,
  TableColumn,
  TableColumnSpec,
  TableColumnType,
  TableSchemaOperation,
} from '../api/types';
import { compareDecimals, decimalDigitCount, isDecimalString, isHttpUrl } from './tableFilter';

/** Server caps (chat/tables/constants.py). */
export const TABLE_TITLE_MAX_LEN = 200;
export const TABLE_DESCRIPTION_MAX_LEN = 500;
export const COLUMN_NAME_MAX_LEN = 80;
export const COLUMN_DESCRIPTION_MAX_LEN = 500;
export const TABLE_MAX_COLUMNS = 100;
export const ENUM_MAX_OPTIONS = 200;
export const ENUM_OPTION_MAX_LEN = 100;
export const NUMBER_MAX_PRECISION = 10;
export const TEXT_MAX_CHARS = 10_000;
export const NUMBER_MAX_DIGITS = 38;
/** Operations in one schema change (constants.py TABLE_SCHEMA_MAX_OPERATIONS). */
export const TABLE_SCHEMA_MAX_OPERATIONS = 200;
export const UNIQUE_COLUMN_TYPES: readonly TableColumnType[] = ['text', 'number', 'url'];
export const COLUMN_TYPES: readonly TableColumnType[] = ['text', 'number', 'url', 'boolean', 'enum'];
/** Server-maintained row fields a column may not be named (any case). */
export const RESERVED_COLUMN_NAMES = ['_id', '_created_at', '_updated_at'];

export interface DraftOption {
  /** Stable React id (never sent). */
  id: string;
  /** The option as loaded, or null for an option the draft adds. */
  original: string | null;
  value: string;
}

export interface DraftColumn {
  /** Stable React id: the column key, or a temp id for a new column. */
  id: string;
  /** The loaded column's key, or null for a column the draft adds. */
  key: string | null;
  name: string;
  type: TableColumnType;
  description: string;
  required: boolean;
  unique: boolean;
  /** text / number / url: the input text; boolean: 'true' / 'false'; '' = no default. */
  defaultValue: string;
  /** enum: the default as option ids (at most one unless `multiple`). */
  defaultOptionIds: string[];
  options: DraftOption[];
  multiple: boolean;
  /** number: the precision input text ('' = none). */
  precision: string;
}

export interface SchemaDraft {
  title: string;
  columns: DraftColumn[];
}

let tempCounter = 0;

/** A fresh temp id for a new column or option (never a column key). */
export function newDraftId(prefix = 'new'): string {
  tempCounter += 1;
  return `${prefix}-${tempCounter}`;
}

function optionId(key: string, index: number): string {
  return `${key}:o${index}`;
}

/** One loaded column as a draft column. */
export function draftColumnFromSchema(column: TableColumn): DraftColumn {
  const options = (column.options ?? []).map((value, index) => ({
    id: optionId(column.key, index),
    original: value,
    value,
  }));
  const defaults = column.default === null ? [] : Array.isArray(column.default) ? column.default : [column.default];
  let defaultValue = '';
  if (column.type === 'boolean' && typeof column.default === 'boolean') {
    defaultValue = column.default ? 'true' : 'false';
  } else if (column.type !== 'enum' && typeof column.default === 'string') {
    defaultValue = column.default;
  }
  return {
    id: column.key,
    key: column.key,
    name: column.name,
    type: column.type,
    description: column.description ?? '',
    required: column.required,
    unique: column.unique,
    defaultValue,
    defaultOptionIds:
      column.type === 'enum'
        ? options.filter((o) => defaults.includes(o.value)).map((o) => o.id)
        : [],
    options,
    multiple: column.multiple,
    precision: column.precision === null || column.precision === undefined ? '' : String(column.precision),
  };
}

/** The draft of a loaded table. */
export function draftFromSchema(title: string, columns: TableColumn[]): SchemaDraft {
  return { title, columns: columns.map(draftColumnFromSchema) };
}

/** A blank new column of `type` (an enum gets one empty option to fill in). */
export function newDraftColumn(name = '', type: TableColumnType = 'text'): DraftColumn {
  return {
    id: newDraftId('col'),
    key: null,
    name,
    type,
    description: '',
    required: false,
    unique: false,
    defaultValue: '',
    defaultOptionIds: [],
    options: type === 'enum' ? [{ id: newDraftId('opt'), original: null, value: '' }] : [],
    multiple: false,
    precision: '',
  };
}

/** The New Table dialog's starting draft: no title, one "Name" text column. */
export function emptyDraft(): SchemaDraft {
  return { title: '', columns: [newDraftColumn('Name', 'text')] };
}

// --- Draft values ---------------------------------------------------------------

/** Python `len` (code points), not UTF-16 units. */
function codePoints(text: string): number {
  return [...text].length;
}

/** Python `str.strip()` closely enough: Unicode whitespace at both ends. */
function strip(text: string): string {
  return text.replace(/^[\s\u0085]+|[\s\u0085]+$/gu, '');
}

/**
 * Case-insensitive comparison key, close to Python `str.casefold()`: the
 * upper-casing pass applies the full case mappings casefold folds with
 * ('ß' -> 'SS', 'ﬁ' -> 'FI'), so 'Straße' and 'STRASSE' collide as on the
 * server; then everything is lower-cased.
 */
export function foldName(text: string): string {
  return text.toUpperCase().toLowerCase();
}

/** The draft column's precision as sent (null = none or not a number column). */
export function draftPrecision(column: DraftColumn): number | null {
  if (column.type !== 'number') return null;
  const text = column.precision.trim();
  if (text === '' || !/^[0-9]+$/.test(text)) return null;
  return Number(text);
}

/** The draft's option values in order, as sent. */
function optionValues(column: DraftColumn): string[] {
  return column.options.map((o) => o.value);
}

/**
 * The draft column's default as the logical value sent to the server (null
 * = none): the input text (number / url trimmed, as the server strips them),
 * a boolean, one option, or the chosen options in option order.
 */
export function draftDefault(column: DraftColumn): TableCellValue {
  switch (column.type) {
    case 'text':
      return column.defaultValue === '' ? null : column.defaultValue;
    case 'number':
    case 'url': {
      const text = column.defaultValue.trim();
      return text === '' ? null : text;
    }
    case 'boolean':
      return column.defaultValue === 'true' ? true : column.defaultValue === 'false' ? false : null;
    case 'enum': {
      const chosen = column.options.filter((o) => column.defaultOptionIds.includes(o.id)).map((o) => o.value);
      if (column.multiple) return chosen.length > 0 ? chosen : null;
      return chosen[0] ?? null;
    }
  }
}

/** Two defaults of the same column type are the same value. */
function sameDefault(type: TableColumnType, a: TableCellValue, b: TableCellValue): boolean {
  if (a === null || b === null) return a === b;
  if (type === 'number' && typeof a === 'string' && typeof b === 'string') {
    const cmp = compareDecimals(a, b);
    return cmp === null ? a === b : cmp === 0;
  }
  if (Array.isArray(a) || Array.isArray(b)) {
    return Array.isArray(a) && Array.isArray(b) && a.length === b.length && a.every((v, i) => v === b[i]);
  }
  return a === b;
}

/** A column spec (POST /tables, add_column) for a new draft column. */
export function draftColumnSpec(column: DraftColumn): TableColumnSpec {
  const spec: TableColumnSpec = { name: column.name, type: column.type };
  const description = strip(column.description);
  if (description) spec.description = description;
  if (column.required) spec.required = true;
  if (column.unique && UNIQUE_COLUMN_TYPES.includes(column.type)) spec.unique = true;
  if (column.type === 'enum') {
    spec.options = optionValues(column);
    if (column.multiple) spec.multiple = true;
  }
  const precision = draftPrecision(column);
  if (precision !== null) spec.precision = precision;
  const value = draftDefault(column);
  if (value !== null) spec.default = value;
  return spec;
}

/**
 * The POST /tables body for a New Table draft. `projectId` makes it a
 * project table (the server gives it the project's mode).
 */
export function draftToCreateRequest(
  draft: SchemaDraft,
  opts: { projectId?: string | null; description?: string } = {},
): CreateTableRequest {
  const body: CreateTableRequest = {
    title: strip(draft.title),
    columns: draft.columns.map(draftColumnSpec),
  };
  const description = strip(opts.description ?? '');
  if (description) body.description = description;
  if (opts.projectId) body.project_id = opts.projectId;
  return body;
}

// --- Validation -----------------------------------------------------------------

export interface ColumnErrors {
  name?: string;
  type?: string;
  description?: string;
  default?: string;
  precision?: string;
  unique?: string;
  multiple?: string;
  /** The option list as a whole (count). */
  options?: string;
  /** Per option id. */
  optionErrors: Record<string, string>;
}

export interface DraftErrors {
  title?: string;
  /** List-level problems (no columns, too many). */
  form?: string;
  /** Per draft column id; only columns with a problem are present. */
  columns: Record<string, ColumnErrors>;
  /** Total count of problems. */
  count: number;
}

export interface ValidateOptions {
  /** The table has no rows (a new required column then needs no default). */
  tableEmpty: boolean;
  /** Validate the title too (the New Table dialog; edits keep the loaded one). */
  checkTitle?: boolean;
}

// schema.py `_text_problem(controls=False)`: Cc, Cf, Zl, Zp, lone surrogates.
const INVISIBLE_RE = /[\p{Cc}\p{Cf}\p{Zl}\p{Zp}\p{Cs}]/u;

function nameProblem(name: string): string | null {
  if (!name || strip(name) === '') return 'Enter a name.';
  if (codePoints(name) > COLUMN_NAME_MAX_LEN) return `Use at most ${COLUMN_NAME_MAX_LEN} characters.`;
  if (name !== strip(name)) return 'Remove the spaces at the start or end.';
  if (INVISIBLE_RE.test(name)) return 'Remove control or invisible characters.';
  if (RESERVED_COLUMN_NAMES.includes(foldName(name))) return `${name} is reserved for a built-in field.`;
  return null;
}

function optionProblem(value: string): string | null {
  if (value === '') return 'Enter the option.';
  if (codePoints(value) > ENUM_OPTION_MAX_LEN) return `Use at most ${ENUM_OPTION_MAX_LEN} characters.`;
  if (value !== strip(value)) return 'Remove the spaces at the start or end.';
  if (INVISIBLE_RE.test(value)) return 'Remove control or invisible characters.';
  return null;
}

/** The problem with the table title, or null (schema.py `validate_table_title`). */
export function titleProblem(title: string): string | null {
  const stripped = strip(title);
  if (!stripped) return 'Enter a title.';
  if (codePoints(stripped) > TABLE_TITLE_MAX_LEN) return `Use at most ${TABLE_TITLE_MAX_LEN} characters.`;
  if (INVISIBLE_RE.test(stripped)) return 'Remove control or invisible characters.';
  return null;
}

function defaultProblem(column: DraftColumn): string | null {
  switch (column.type) {
    case 'text': {
      const value = column.defaultValue;
      if (value.includes('\u0000')) return 'Remove the NUL character.';
      if (codePoints(value.replace(/\r\n?/g, '\n')) > TEXT_MAX_CHARS) {
        return `Use at most ${TEXT_MAX_CHARS} characters.`;
      }
      return null;
    }
    case 'number': {
      const value = column.defaultValue.trim();
      if (value === '') return null;
      if (!isDecimalString(value)) return 'Enter a plain number, e.g. 12.5.';
      if (draftPrecision(column) === null && (decimalDigitCount(value) ?? 0) > NUMBER_MAX_DIGITS) {
        return `Use at most ${NUMBER_MAX_DIGITS} digits.`;
      }
      return null;
    }
    case 'url': {
      const value = column.defaultValue.trim();
      if (value === '') return null;
      return isHttpUrl(value) ? null : 'Enter an http:// or https:// URL.';
    }
    case 'boolean':
      return null;
    case 'enum': {
      const ids = new Set(column.options.map((o) => o.id));
      if (column.defaultOptionIds.some((id) => !ids.has(id))) return 'The default uses a removed option.';
      if (!column.multiple && column.defaultOptionIds.length > 1) return 'Choose one default option.';
      return null;
    }
  }
}

/**
 * Every problem of the draft against the server's rules (see the module
 * docstring), each at its field. `before` is the loaded schema ([] for a
 * new table).
 */
export function validateDraft(
  before: TableColumn[],
  draft: SchemaDraft,
  opts: ValidateOptions,
): DraftErrors {
  const errors: DraftErrors = { columns: {}, count: 0 };
  const beforeByKey = new Map(before.map((c) => [c.key, c]));

  if (opts.checkTitle) {
    const problem = titleProblem(draft.title);
    if (problem) {
      errors.title = problem;
      errors.count += 1;
    }
  }
  if (draft.columns.length === 0) {
    errors.form = 'A table needs at least one column.';
    errors.count += 1;
  } else if (draft.columns.length > TABLE_MAX_COLUMNS) {
    errors.form = `A table can have at most ${TABLE_MAX_COLUMNS} columns.`;
    errors.count += 1;
  }

  const nameCounts = new Map<string, number>();
  for (const column of draft.columns) {
    const folded = foldName(column.name);
    nameCounts.set(folded, (nameCounts.get(folded) ?? 0) + 1);
  }

  for (const column of draft.columns) {
    const col: ColumnErrors = { optionErrors: {} };
    let count = 0;
    const set = (field: Exclude<keyof ColumnErrors, 'optionErrors'>, message: string) => {
      if (col[field] === undefined) {
        col[field] = message;
        count += 1;
      }
    };
    const loaded = column.key !== null ? beforeByKey.get(column.key) : undefined;

    const nameIssue = nameProblem(column.name);
    if (nameIssue) set('name', nameIssue);
    else if ((nameCounts.get(foldName(column.name)) ?? 0) > 1) {
      set('name', 'Another column has this name (names ignore case).');
    }

    if (!COLUMN_TYPES.includes(column.type)) set('type', 'Choose a type.');
    else if (loaded && loaded.type !== column.type) set('type', "Type can't be changed after creation.");

    const description = strip(column.description);
    if (codePoints(description) > COLUMN_DESCRIPTION_MAX_LEN) {
      set('description', `Use at most ${COLUMN_DESCRIPTION_MAX_LEN} characters.`);
    } else if (description.includes('\u0000')) {
      set('description', 'Remove the NUL character.');
    }

    if (column.unique && !UNIQUE_COLUMN_TYPES.includes(column.type)) {
      set('unique', 'Only text, number and URL columns can be unique.');
    }

    const precisionText = column.precision.trim();
    if (precisionText !== '') {
      if (column.type !== 'number') set('precision', 'Only number columns have a precision.');
      else if (!/^[0-9]+$/.test(precisionText) || Number(precisionText) > NUMBER_MAX_PRECISION) {
        set('precision', `Use a whole number from 0 to ${NUMBER_MAX_PRECISION}.`);
      }
    }

    if (column.multiple && column.type !== 'enum') set('multiple', 'Only choice columns allow several options.');
    if (loaded && loaded.multiple && !column.multiple) {
      set('multiple', "A column that allows several options can't go back to one.");
    }

    if (column.type === 'enum') {
      if (column.options.length === 0) set('options', 'Add at least one option.');
      else if (column.options.length > ENUM_MAX_OPTIONS) set('options', `Use at most ${ENUM_MAX_OPTIONS} options.`);
      const seen = new Map<string, number>();
      for (const option of column.options) {
        const folded = foldName(option.value);
        seen.set(folded, (seen.get(folded) ?? 0) + 1);
      }
      for (const option of column.options) {
        const problem =
          optionProblem(option.value) ??
          ((seen.get(foldName(option.value)) ?? 0) > 1 ? 'This option is listed twice (options ignore case).' : null);
        if (problem) {
          col.optionErrors[option.id] = problem;
          count += 1;
        }
      }
    }

    const defaultIssue = defaultProblem(column);
    if (defaultIssue) set('default', defaultIssue);
    else if (
      column.key === null &&
      column.required &&
      !opts.tableEmpty &&
      draftDefault(column) === null
    ) {
      // schema_ops.py `_add_column`: required_needs_default.
      set('default', 'A required column needs a default while the table has rows.');
    }

    if (count > 0) {
      errors.columns[column.id] = col;
      errors.count += count;
    }
  }
  return errors;
}

// --- Re-added options -------------------------------------------------------------

/**
 * New options of a loaded enum column that spell (case-insensitively, as the
 * server compares options) an option the draft removed: option id -> the
 * loaded spelling it stands for. Such an option is the loaded one kept (or
 * renamed, when the case differs), not a removal plus an add -- which the
 * server refuses (rows_violate) while rows still use the option. The server
 * cannot move an existing option (and appends a new one that has nothing
 * before it), so with `inPlaceOnly` an option is claimed only where the
 * loaded one sits among the kept options and no new option comes before
 * all of them (the draft's order then still holds); `settleReaddedOptions`
 * moves the others into place.
 */
function readdedOptionClaims(
  column: DraftColumn,
  loadedOptions: readonly string[],
  inPlaceOnly: boolean,
): Map<string, string> {
  const claims = new Map<string, string>();
  const index = new Map(loadedOptions.map((o, i) => [o, i]));
  const keptAt = column.options.map((o) => (o.original !== null && index.has(o.original) ? index.get(o.original)! : -1));
  const kept = new Set(column.options.filter((_, i) => keptAt[i] >= 0).map((o) => o.original as string));
  const removed = loadedOptions.filter((o) => !kept.has(o));
  if (removed.length === 0) return claims;
  const taken = new Set<string>();
  let floor = -1;
  column.options.forEach((option, i) => {
    if (keptAt[i] >= 0) {
      floor = Math.max(floor, keptAt[i]);
      return;
    }
    const folded = foldName(option.value);
    const loaded = removed.find((o) => !taken.has(o) && foldName(o) === folded);
    if (loaded === undefined || option.value === '') return;
    if (inPlaceOnly) {
      const at = index.get(loaded)!;
      const next = keptAt.slice(i + 1).find((k) => k >= 0);
      // New options before it and no loaded one: those would be appended.
      if (floor < 0 && i > 0) return;
      if (at <= floor || (next !== undefined && at >= next)) return;
      floor = at;
    }
    claims.set(option.id, loaded);
    taken.add(loaded);
  });
  return claims;
}

/**
 * The column with every new option that spells a removed loaded option
 * turned back into that option (`original` set) and moved to its loaded
 * place among the kept options (see `readdedOptionClaims`). The editor
 * applies it when an option field of a loaded column loses focus, so
 * removing an option and typing it again keeps it -- and its rows. Returns
 * `column` itself when there is nothing to settle.
 */
export function settleReaddedOptions(column: DraftColumn, loaded: TableColumn | null | undefined): DraftColumn {
  if (!loaded || loaded.type !== 'enum' || column.type !== 'enum') return column;
  const loadedOptions = loaded.options ?? [];
  const claims = readdedOptionClaims(column, loadedOptions, false);
  if (claims.size === 0) return column;
  const inPlace = readdedOptionClaims(column, loadedOptions, true);
  const index = new Map(loadedOptions.map((o, i) => [o, i]));
  let options = column.options.map((o) => (claims.has(o.id) ? { ...o, original: claims.get(o.id)! } : o));
  const anchored = (o: DraftOption) => o.original !== null && index.has(o.original);
  for (const [id, original] of claims) {
    if (inPlace.has(id)) continue;
    const at = index.get(original)!;
    const option = options.find((o) => o.id === id)!;
    const rest = options.filter((o) => o.id !== id);
    // Before the first loaded option that comes after it, else after the
    // last loaded one, else first.
    let to = rest.findIndex((o) => anchored(o) && index.get(o.original as string)! > at);
    if (to < 0) {
      let last = -1;
      rest.forEach((o, i) => {
        if (anchored(o)) last = i;
      });
      to = last + 1;
    }
    options = [...rest.slice(0, to), option, ...rest.slice(to)];
  }
  return { ...column, options };
}

// --- Operations -----------------------------------------------------------------

export interface DerivedSchemaChange {
  operations: TableSchemaOperation[];
  /**
   * Per operation, the draft column id it acts on (null for rename_table and
   * reorder_columns; the loaded key of a column being dropped, which the
   * draft no longer has), so a server error's `op_index` can be shown at
   * its column.
   */
  targets: (string | null)[];
}

/** A name not used (case-insensitively) by any of `taken`. */
function temporaryName(taken: Iterable<string>, base: string): string {
  const folded = new Set([...taken].map(foldName));
  for (let n = 1; ; n += 1) {
    const candidate = `${base} ${n}`;
    if (!folded.has(foldName(candidate))) return candidate;
  }
}

interface Rename {
  id: string;
  from: string;
  to: string;
}

/**
 * Order renames so none collides (case-insensitively) with a name in use at
 * that point: a rename runs once its target is free. When every target is
 * held, the renames form cycles (A -> B while B -> A): one item whose name
 * another rename wants is parked under a temporary name (each item at most
 * once), which frees that name. `current` maps id -> name now (every item
 * in the namespace, renamed or not) and is updated in place. A target held
 * by an item that keeps its name only happens in an invalid draft (two
 * equal names); those renames are emitted as they are, for the server to
 * refuse, so the loop always ends. Returns the steps in order.
 */
function orderRenames(current: Map<string, string>, renames: Rename[], tempBase: string): Rename[] {
  const steps: Rename[] = [];
  const pending = renames.filter((r) => current.get(r.id) !== r.to);
  const parked = new Set<string>();
  const held = (name: string, exceptId: string) =>
    [...current].some(([id, n]) => id !== exceptId && foldName(n) === foldName(name));
  const step = (id: string, to: string) => {
    steps.push({ id, from: current.get(id)!, to });
    current.set(id, to);
  };
  while (pending.length > 0) {
    const ready = pending.findIndex((r) => !held(r.to, r.id));
    if (ready >= 0) {
      const [rename] = pending.splice(ready, 1);
      step(rename.id, rename.to);
      continue;
    }
    const blocker = pending.find(
      (r) =>
        !parked.has(r.id) &&
        pending.some((other) => other !== r && foldName(other.to) === foldName(current.get(r.id)!)),
    );
    if (!blocker) {
      for (const rename of pending) step(rename.id, rename.to);
      break;
    }
    parked.add(blocker.id);
    step(blocker.id, temporaryName([...current.values(), ...pending.map((r) => r.to)], tempBase));
  }
  return steps;
}

/** A default as a list of options (empty for none). */
function defaultOptions(value: TableCellValue): string[] {
  if (value === null) return [];
  return Array.isArray(value) ? value : [value as string];
}

/** A default with option values, canonical for comparison (multi: by option order). */
function canonicalEnumDefault(value: TableCellValue, options: string[], multiple: boolean): TableCellValue {
  if (value === null) return null;
  if (!multiple) return value;
  const order = new Map(options.map((o, i) => [o, i]));
  const sorted = [...new Set(defaultOptions(value))].sort((a, b) => (order.get(a) ?? 1e9) - (order.get(b) ?? 1e9));
  return sorted.length > 0 ? sorted : null;
}

/**
 * The operations turning the loaded schema (`before`, titled `beforeTitle`)
 * into `draft`, in an order the server accepts (see the module docstring),
 * with the draft column each acts on. Empty when nothing changed. The draft
 * is assumed valid (`validateDraft`); an invalid draft still derives
 * without throwing, and the server refuses it.
 */
export function deriveSchemaChange(
  before: TableColumn[],
  beforeTitle: string,
  draft: SchemaDraft,
): DerivedSchemaChange {
  const operations: TableSchemaOperation[] = [];
  const targets: (string | null)[] = [];
  const push = (op: TableSchemaOperation, target: string | null) => {
    operations.push(op);
    targets.push(target);
  };

  // 1. Title.
  const title = strip(draft.title);
  if (title !== '' && title !== beforeTitle) push({ op: 'rename_table', title }, null);

  const beforeByKey = new Map(before.map((c) => [c.key, c]));
  const existing = draft.columns.filter(
    (c): c is DraftColumn & { key: string } => c.key !== null && beforeByKey.has(c.key),
  );
  const keptKeys = new Set(existing.map((c) => c.key));
  const added = draft.columns.filter((c) => !(c.key !== null && keptKeys.has(c.key)));

  // Working copy of the surviving columns: order (draft ids; an existing
  // column's id is its key) and names.
  const order: string[] = before.filter((c) => keptKeys.has(c.key)).map((c) => c.key);
  const names = new Map(before.filter((c) => keptKeys.has(c.key)).map((c) => [c.key, c.name]));

  // 2. Drops, by loaded name, first (their names are then free for renames
  // and adds). A table keeps at least one column (last_column): when the
  // draft keeps none of the loaded ones, one drop waits until the first new
  // column is in. It is one whose name the first new column does not take,
  // else it is parked under a temporary name first.
  const dropped = before.filter((c) => !keptKeys.has(c.key));
  let lastDrop: { key: string; name: string } | null = null;
  if (existing.length === 0 && dropped.length > 0 && added.length > 0) {
    const firstName = foldName(added[0].name);
    const pick = [...dropped].reverse().find((c) => foldName(c.name) !== firstName) ?? dropped[dropped.length - 1];
    lastDrop = { key: pick.key, name: pick.name };
  }
  for (const column of dropped) {
    if (column.key !== lastDrop?.key) push({ op: 'drop_column', name: column.name }, column.key);
  }
  if (lastDrop && foldName(lastDrop.name) === foldName(added[0].name)) {
    const temp = temporaryName([lastDrop.name, ...draft.columns.map((c) => c.name)], 'Removing column');
    push({ op: 'rename_column', name: lastDrop.name, new_name: temp }, lastDrop.key);
    lastDrop.name = temp;
  }

  // 3. Column renames.
  const renameSteps = orderRenames(
    names,
    existing.map((c) => ({ id: c.key, from: beforeByKey.get(c.key)!.name, to: c.name })),
    'Renaming column',
  );
  for (const step of renameSteps) {
    push({ op: 'rename_column', name: step.from, new_name: step.to }, step.id);
  }

  // 4. Per surviving column, addressed by its new name.
  for (const column of existing) {
    const loaded = beforeByKey.get(column.key)!;
    const name = column.name;
    const target = column.id;

    const description = strip(column.description);
    if (description !== (loaded.description ?? '')) {
      push({ op: 'set_description', name, description }, target);
    }
    if (column.required !== loaded.required) {
      push({ op: 'set_required', name, required: column.required }, target);
    }
    const unique = column.unique && UNIQUE_COLUMN_TYPES.includes(loaded.type);
    if (unique !== loaded.unique) push({ op: 'set_unique', name, unique }, target);
    if (loaded.type === 'number') {
      const precision = draftPrecision(column);
      if (precision !== loaded.precision) push({ op: 'set_precision', name, precision }, target);
    }

    let workingDefault: TableCellValue = loaded.default;
    if (loaded.type === 'enum') {
      let multiple = loaded.multiple;
      if (column.multiple && !loaded.multiple) {
        push({ op: 'set_enum_multiple', name, multiple: true }, target);
        multiple = true;
        workingDefault = workingDefault === null ? null : [workingDefault as string];
      }

      const loadedOptions = loaded.options ?? [];
      // The loaded option each draft option stands for (null: a new one),
      // counting a re-typed removed option as kept (readdedOptionClaims).
      const claims = readdedOptionClaims(column, loadedOptions, true);
      const originalOf = (o: DraftOption): string | null =>
        claims.get(o.id) ?? (o.original !== null && loadedOptions.includes(o.original) ? o.original : null);
      const kept = new Set(
        column.options.map(originalOf).filter((o): o is string => o !== null),
      );
      const removed = loadedOptions.filter((o) => !kept.has(o));
      const fresh = column.options.filter((o) => originalOf(o) === null);

      // The default must not hold an option about to be removed
      // (default_uses_option): change it first -- straight to the wanted
      // default when every option it needs is a kept one (named as loaded:
      // renames come later and carry the default along), else to none until
      // the adds are in.
      if (defaultOptions(workingDefault).some((o) => removed.includes(o))) {
        const chosen = column.options.filter((o) => column.defaultOptionIds.includes(o.id));
        let value: TableCellValue = null;
        if (chosen.length > 0 && chosen.every((o) => originalOf(o) !== null)) {
          const spelled = chosen.map((o) => originalOf(o) as string);
          value = multiple ? canonicalEnumDefault(spelled, loadedOptions, true) : spelled[0];
        }
        push({ op: 'set_default', name, default: value }, target);
        workingDefault = value;
      }

      // Removals before renames and adds, so a freed spelling can be reused.
      // An enum keeps at least one option (last_option): when none of the
      // loaded ones stays, one removal waits until the first new option is
      // in (parked under a temporary spelling if that option takes it).
      let lastRemoval: string | null = null;
      if (kept.size === 0 && removed.length > 0 && fresh.length > 0) {
        const firstValue = foldName(fresh[0].value);
        lastRemoval = [...removed].reverse().find((o) => foldName(o) !== firstValue) ?? removed[removed.length - 1];
      }
      for (const option of removed) {
        if (option !== lastRemoval) push({ op: 'remove_enum_option', name, option }, target);
      }
      if (lastRemoval !== null && foldName(lastRemoval) === foldName(fresh[0].value)) {
        const temp = temporaryName([lastRemoval, ...column.options.map((o) => o.value)], 'Removing option');
        push({ op: 'rename_enum_option', name, option: lastRemoval, new_option: temp }, target);
        lastRemoval = temp;
      }

      // Option renames, keyed by their loaded spelling.
      const working = new Map<string, string>();
      for (const option of loadedOptions) if (kept.has(option)) working.set(option, option);
      const optionSteps = orderRenames(
        working,
        column.options
          .filter((o) => originalOf(o) !== null)
          .map((o) => ({ id: originalOf(o) as string, from: originalOf(o) as string, to: o.value })),
        'Renaming option',
      );
      for (const step of optionSteps) {
        push({ op: 'rename_enum_option', name, option: step.from, new_option: step.to }, target);
        if (workingDefault !== null) {
          workingDefault = Array.isArray(workingDefault)
            ? workingDefault.map((o) => (o === step.from ? step.to : o))
            : workingDefault === step.from
              ? step.to
              : workingDefault;
        }
      }

      // Adds, each after the option before it in the draft (appended when
      // it is first).
      column.options.forEach((option, index) => {
        if (!fresh.includes(option)) return;
        const previous = index > 0 ? column.options[index - 1].value : null;
        push(
          previous === null
            ? { op: 'add_enum_option', name, option: option.value }
            : { op: 'add_enum_option', name, option: option.value, after: previous },
          target,
        );
        if (lastRemoval !== null) {
          push({ op: 'remove_enum_option', name, option: lastRemoval }, target);
          lastRemoval = null;
        }
      });

      // The default, when what the steps above left differs (after the adds,
      // so a new option can be the default).
      const finalOptions = optionValues(column);
      const wanted = canonicalEnumDefault(draftDefault(column), finalOptions, multiple);
      const have = canonicalEnumDefault(workingDefault, finalOptions, multiple);
      if (!sameDefault('enum', wanted, have)) push({ op: 'set_default', name, default: wanted }, target);
    } else {
      const wanted = draftDefault(column);
      // The form cannot hold an empty-string text default: it reads as none.
      if (loaded.type === 'text' && workingDefault === '') workingDefault = null;
      if (!sameDefault(loaded.type, wanted, workingDefault)) {
        push({ op: 'set_default', name, default: wanted }, target);
      }
    }
  }

  // 5. New columns in draft order, each after the column before it in the
  // draft (appended when it is first, or follows nothing placed yet).
  for (const column of added) {
    const index = draft.columns.indexOf(column);
    const previous = index > 0 ? draft.columns[index - 1] : null;
    const spec = draftColumnSpec(column);
    if (previous && order.includes(previous.id)) {
      push({ op: 'add_column', column: spec, after: previous.name }, column.id);
      order.splice(order.indexOf(previous.id) + 1, 0, column.id);
    } else {
      push({ op: 'add_column', column: spec }, column.id);
      order.push(column.id);
    }
    if (lastDrop) {
      push({ op: 'drop_column', name: lastDrop.name }, lastDrop.key);
      lastDrop = null;
    }
  }

  // 6. Final order, with the final names, only when it differs.
  const finalIds = draft.columns.map((c) => c.id);
  if (finalIds.length === order.length && finalIds.some((id, i) => id !== order[i])) {
    push({ op: 'reorder_columns', names: draft.columns.map((c) => c.name) }, null);
  }

  return { operations, targets };
}

/** The operations of `deriveSchemaChange` alone. */
export function deriveOperations(
  before: TableColumn[],
  beforeTitle: string,
  draft: SchemaDraft,
): TableSchemaOperation[] {
  return deriveSchemaChange(before, beforeTitle, draft).operations;
}

/** What a derived change does, counted the way the user edited it. */
export interface SchemaChangeSummary {
  /** Loaded columns with at least one changed setting (name, options, ...). */
  changed: number;
  added: number;
  removed: number;
  /** The column order changes. */
  reordered: boolean;
  /** The table title changes. */
  retitled: boolean;
}

/**
 * Count `change` (from `deriveSchemaChange` over `draft`) per edited thing
 * rather than per operation: a swap of two names is two changed columns,
 * not the three renames its temporary name needs.
 */
export function summarizeSchemaChange(change: DerivedSchemaChange, draft: SchemaDraft): SchemaChangeSummary {
  const loadedIds = new Set(draft.columns.filter((c) => c.key !== null).map((c) => c.id));
  const changed = new Set<string>();
  const summary: SchemaChangeSummary = { changed: 0, added: 0, removed: 0, reordered: false, retitled: false };
  change.operations.forEach((op, i) => {
    const target = change.targets[i];
    if (op.op === 'add_column') summary.added += 1;
    else if (op.op === 'drop_column') summary.removed += 1;
    else if (op.op === 'reorder_columns') summary.reordered = true;
    else if (op.op === 'rename_table') summary.retitled = true;
    else if (target !== null && loadedIds.has(target)) changed.add(target);
  });
  summary.changed = changed.size;
  return summary;
}

/**
 * The summary as a phrase, e.g. "2 columns changed, 1 column added, new
 * column order" ('' when nothing changes).
 */
export function schemaChangePhrase(summary: SchemaChangeSummary): string {
  const columns = (n: number) => `${n} ${n === 1 ? 'column' : 'columns'}`;
  const parts: string[] = [];
  if (summary.changed > 0) parts.push(`${columns(summary.changed)} changed`);
  if (summary.added > 0) parts.push(`${columns(summary.added)} added`);
  if (summary.removed > 0) parts.push(`${columns(summary.removed)} removed`);
  if (summary.reordered) parts.push('new column order');
  if (summary.retitled) parts.push('new title');
  const text = parts.join(', ');
  return text.charAt(0).toUpperCase() + text.slice(1);
}

// --- Re-basing after a conflict ---------------------------------------------------

export interface RebasedDraft {
  draft: SchemaDraft;
  /** One line per edit that could not be kept (its column or option is gone). */
  dropped: string[];
}

function columnChanged(a: DraftColumn, b: DraftColumn, field: keyof DraftColumn): boolean {
  const x = a[field];
  const y = b[field];
  if (Array.isArray(x) && Array.isArray(y)) return JSON.stringify(x) !== JSON.stringify(y);
  return x !== y;
}

/**
 * Rebuild `draft` (edited over `oldBase`) on top of `newBase` (the schema
 * someone else saved meanwhile): a column the user edited keeps each field
 * the user changed when the column still exists (by key), as does each
 * option rename / removal whose option still exists; the user's new columns
 * and options are kept; drops still apply. Edits to a column or option that
 * no longer exists are dropped and listed. The column order follows the new
 * schema unless the user reordered, in which case the user's order wins and
 * columns the user did not know about go last.
 */
export function rebaseDraft(
  oldBase: TableColumn[],
  draft: SchemaDraft,
  newTitle: string,
  newBase: TableColumn[],
  oldTitle: string,
): RebasedDraft {
  const dropped: string[] = [];
  const oldDraft = draftFromSchema(oldTitle, oldBase);
  const oldById = new Map(oldDraft.columns.map((c) => [c.id, c]));
  const newById = new Map(newBase.map((c) => [c.key, draftColumnFromSchema(c)]));
  const draftKeys = new Set(draft.columns.filter((c) => c.key !== null).map((c) => c.key as string));

  const rebuilt = new Map<string, DraftColumn>();
  for (const column of draft.columns) {
    if (column.key === null) {
      rebuilt.set(column.id, column);
      continue;
    }
    const before = oldById.get(column.id);
    const fresh = newById.get(column.key);
    if (!fresh) {
      if (before && JSON.stringify(before) !== JSON.stringify(column)) {
        dropped.push(`Column ${column.name} was removed meanwhile; your edits to it were dropped.`);
      }
      continue;
    }
    if (!before) {
      rebuilt.set(column.id, fresh);
      continue;
    }
    const next: DraftColumn = { ...fresh };
    for (const field of ['name', 'description', 'required', 'unique', 'defaultValue', 'multiple', 'precision'] as const) {
      if (columnChanged(before, column, field)) (next as unknown as Record<string, unknown>)[field] = column[field];
    }
    if (fresh.type === 'enum') {
      const freshByOriginal = new Map(fresh.options.map((o) => [o.original as string, o]));
      const userByOriginal = new Map(
        column.options.filter((o) => o.original !== null).map((o) => [o.original as string, o]),
      );
      const options: DraftOption[] = [];
      for (const option of fresh.options) {
        const userOption = userByOriginal.get(option.original as string);
        const wasLoaded = (before.options ?? []).some((o) => o.original === option.original);
        if (wasLoaded && !userOption) continue; // the user removed it
        options.push(userOption ? { ...option, value: userOption.value } : option);
      }
      for (const option of column.options) {
        if (option.original === null) options.push(option);
        else if (!freshByOriginal.has(option.original) && option.value !== option.original) {
          dropped.push(`Option ${option.original} of ${column.name} was removed meanwhile; your rename was dropped.`);
        }
      }
      next.options = options;
      // The default: the user's choice when they changed it, mapped onto
      // the options that still exist.
      if (columnChanged(before, column, 'defaultOptionIds')) {
        const chosen = column.options.filter((o) => column.defaultOptionIds.includes(o.id));
        next.defaultOptionIds = chosen
          .map((o) => (o.original === null ? o.id : freshByOriginal.get(o.original)?.id ?? null))
          .filter((id): id is string => id !== null && options.some((o) => o.id === id));
      } else {
        next.defaultOptionIds = fresh.defaultOptionIds.filter((id) => options.some((o) => o.id === id));
      }
    }
    rebuilt.set(column.id, next);
  }

  // Order.
  const userExistingOrder = draft.columns.filter((c) => c.key !== null).map((c) => c.id);
  const oldOrder = oldBase.map((c) => c.key).filter((key) => draftKeys.has(key));
  const reordered = userExistingOrder.join('\u0000') !== oldOrder.join('\u0000');
  let ids: string[];
  if (reordered) {
    ids = draft.columns.map((c) => c.id).filter((id) => rebuilt.has(id));
    for (const column of newBase) {
      if (!oldById.has(column.key) && !ids.includes(column.key)) {
        ids.push(column.key);
        rebuilt.set(column.key, newById.get(column.key)!);
      }
    }
  } else {
    ids = [];
    for (const column of newBase) {
      const known = oldById.has(column.key);
      if (known && !draftKeys.has(column.key)) continue; // the user dropped it
      if (!known) rebuilt.set(column.key, newById.get(column.key)!);
      ids.push(column.key);
    }
    // The user's new columns after the column they followed in the draft.
    draft.columns.forEach((column, index) => {
      if (column.key !== null) return;
      const previous = index > 0 ? draft.columns[index - 1].id : null;
      const at = previous === null ? -1 : ids.indexOf(previous);
      if (previous !== null && at < 0) ids.push(column.id);
      else ids.splice(at + 1, 0, column.id);
    });
  }

  const title = draft.title !== oldTitle ? draft.title : newTitle;
  return { draft: { title, columns: ids.map((id) => rebuilt.get(id)!) }, dropped };
}

/** True when the draft differs from its loaded schema (any operation). */
export function draftIsDirty(before: TableColumn[], beforeTitle: string, draft: SchemaDraft): boolean {
  return deriveSchemaChange(before, beforeTitle, draft).operations.length > 0;
}
