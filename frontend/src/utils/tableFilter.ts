/**
 * Quest Tables filter bar logic (pure): the builder state, its mapping to the
 * query_rows filter DSL (spec 6.1, chat/tables/filters.py), validation, and
 * the `?filter=` / `?sort=` URL encoding.
 *
 * Builder state: a list of `FilterCondition`s joined with AND, one chip each.
 * A condition names its column by NAME (as the DSL does; keys are never
 * shown or sent). `conditionsToFilter` emits `null` for no conditions, the
 * bare leaf for one, else `{"and": [...]}`; `filterToConditions` is the
 * inverse for exactly those shapes (and `{"and": [leaf]}`). Anything else --
 * `or`, `not`, nesting, an unknown column, an op the bar does not offer for
 * the column type, or a value it would refuse -- is reported as `custom` so
 * the UI shows "Custom filter" with a Clear button instead of dropping it.
 *
 * Operand shapes match filters.py `_parse_leaf`:
 * - nullary ops (`is_null`, `not_null`, `is_empty`) carry no `value` key.
 * - numbers are canonical decimal strings (trimmed, the server's plain
 *   decimal grammar: no thousands separators, units or NaN, at most 38
 *   significant digits); `between` is `[lo, hi]` with lo <= hi, compared
 *   exactly as decimals (`compareDecimals`).
 * - text operands are sent exactly as typed (never trimmed). A url column's
 *   `eq` / `ne` / `in` operands must be http(s) URLs with a host
 *   (`isHttpUrl`, schema.py `_coerce_url`); its substring ops take any text.
 * - booleans are JSON booleans, never strings.
 * - enum options exactly as the schema spells them; a multi-enum `eq` / `ne`
 *   operand is itself a list of options (the exact set), `contains` one
 *   option, `contains_any` / `contains_all` a non-empty list.
 * - `in`, `contains_any` and `contains_all` are non-empty lists of at most
 *   100 values (`TABLE_FILTER_IN_MAX_VALUES`); only a multi-enum `eq` / `ne`
 *   operand (one exact option set) may hold up to 200 options. A
 *   multi-enum `in` (a list of lists) is valid DSL but not offered: it
 *   reads as custom.
 *
 * Round trip: for every leaf `filterToConditions` accepts as a regular chip,
 * conditions -> DSL -> conditions -> DSL is the identity, so re-emitting an
 * untouched chip (when another chip is edited) never changes it.
 *
 * URL: the params hold the DSL JSON itself (what the rows API takes), so a
 * filtered link is the API filter verbatim. Encoding is
 * encodeURIComponent / URLSearchParams, so `+`, `&`, `#`, `%`, quotes and
 * non-ASCII text survive the round trip (utils/tablesRoute.ts).
 */

import type { FilterNode, FilterOp, SortKey, TableColumn } from '../api/types';

/** Server caps (chat/tables/constants.py). */
export const FILTER_MAX_CONDITIONS = 20;
export const FILTER_IN_MAX_VALUES = 100;
export const FILTER_ENUM_MAX_OPTIONS = 200;
export const SORT_MAX_KEYS = 3;
export const NUMBER_MAX_DIGITS = 38;
export const URL_MAX_CHARS = 2048;

/** A condition's operand in builder state (see `valueKind`). */
export type FilterValue = null | string | boolean | string[] | boolean[];

export interface FilterCondition {
  /** Stable id for React keys and chip editing (never sent). */
  id: string;
  /** Column NAME. */
  column: string;
  op: FilterOp;
  value: FilterValue;
}

/**
 * The input a condition's value needs:
 * - `none`: no value (nullary ops).
 * - `text`: one string (text / url).
 * - `number`: one decimal string.
 * - `pair`: `[lo, hi]` decimal strings (between).
 * - `boolean`: true / false.
 * - `booleans`: a list of booleans (boolean `in`).
 * - `option`: one enum option.
 * - `options`: a list of enum options.
 * - `lines`: a list of strings typed one per line (text / url / number `in`).
 */
export type ValueKind =
  | 'none'
  | 'text'
  | 'number'
  | 'pair'
  | 'boolean'
  | 'booleans'
  | 'option'
  | 'options'
  | 'lines';

export interface OpChoice {
  op: FilterOp;
  label: string;
}

const NULL_OPS: OpChoice[] = [
  { op: 'is_null', label: 'is empty' },
  { op: 'not_null', label: 'is not empty' },
];

const TEXT_OPS: OpChoice[] = [
  { op: 'eq', label: 'is' },
  { op: 'ne', label: 'is not' },
  { op: 'contains', label: 'contains' },
  { op: 'starts_with', label: 'starts with' },
  { op: 'ends_with', label: 'ends with' },
  { op: 'in', label: 'is any of' },
  ...NULL_OPS,
];

const NUMBER_OPS: OpChoice[] = [
  { op: 'eq', label: '=' },
  { op: 'ne', label: '≠' },
  { op: 'lt', label: '<' },
  { op: 'lte', label: '≤' },
  { op: 'gt', label: '>' },
  { op: 'gte', label: '≥' },
  { op: 'between', label: 'between' },
  { op: 'in', label: 'is any of' },
  ...NULL_OPS,
];

const SINGLE_OPS: OpChoice[] = [
  { op: 'eq', label: 'is' },
  { op: 'ne', label: 'is not' },
  { op: 'in', label: 'is any of' },
  ...NULL_OPS,
];

const MULTI_OPS: OpChoice[] = [
  { op: 'contains', label: 'has' },
  { op: 'contains_any', label: 'has any of' },
  { op: 'contains_all', label: 'has all of' },
  { op: 'eq', label: 'is exactly' },
  { op: 'ne', label: 'is not exactly' },
  { op: 'is_empty', label: 'is empty' },
  { op: 'not_null', label: 'is not empty' },
];

/** The ops the bar offers for a column, in menu order, with labels. */
export function opsForColumn(column: TableColumn): OpChoice[] {
  switch (column.type) {
    case 'text':
    case 'url':
      return TEXT_OPS;
    case 'number':
      return NUMBER_OPS;
    case 'enum':
      return column.multiple ? MULTI_OPS : SINGLE_OPS;
    default:
      return SINGLE_OPS;
  }
}

/** The label of `op` on `column`, or the raw op when not offered. */
export function opLabel(column: TableColumn, op: FilterOp): string {
  return opsForColumn(column).find((choice) => choice.op === op)?.label ?? op;
}

/** The value input `op` takes on `column`. */
export function valueKind(column: TableColumn, op: FilterOp): ValueKind {
  if (op === 'is_null' || op === 'not_null' || op === 'is_empty') return 'none';
  const multi = column.type === 'enum' && column.multiple;
  if (multi) {
    return op === 'contains' ? 'option' : 'options';
  }
  if (op === 'in') {
    if (column.type === 'boolean') return 'booleans';
    if (column.type === 'enum') return 'options';
    return 'lines';
  }
  switch (column.type) {
    case 'number':
      return op === 'between' ? 'pair' : 'number';
    case 'boolean':
      return 'boolean';
    case 'enum':
      return 'option';
    default:
      return 'text';
  }
}

/** The empty value a fresh condition starts from for `kind`. */
export function emptyValue(kind: ValueKind): FilterValue {
  switch (kind) {
    case 'none':
    case 'boolean':
      return null;
    case 'text':
    case 'number':
    case 'option':
      return '';
    case 'pair':
      return ['', ''];
    default:
      return [];
  }
}

/** The server's plain decimal grammar (schema.py `_NUMBER_RE`). */
const DECIMAL_RE = /^[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?$/;

/** A trimmed plain decimal string ("1,234", "1e", "NaN" are not). */
export function isDecimalString(value: string): boolean {
  return DECIMAL_RE.test(value.trim());
}

interface DecimalParts {
  negative: boolean;
  /** Significant digits, no leading or trailing zeros ('' for zero). */
  digits: string;
  /** value = digits * 10^exponent. */
  exponent: bigint;
}

/** A decimal string (`isDecimalString`) split exactly, or null. */
function decimalParts(value: string): DecimalParts | null {
  const text = value.trim();
  if (!DECIMAL_RE.test(text)) return null;
  const negative = text.startsWith('-');
  const unsigned = text.replace(/^[+-]/, '');
  const [mantissa, exp = '0'] = unsigned.split(/[eE]/);
  const [intPart, fracPart = ''] = mantissa.split('.');
  const all = `${intPart}${fracPart}`;
  const lead = all.replace(/^0+/, '');
  const digits = lead.replace(/0+$/, '');
  if (digits === '') return { negative: false, digits: '', exponent: 0n };
  const trailing = BigInt(lead.length - digits.length);
  return { negative, digits, exponent: BigInt(exp) - BigInt(fracPart.length) + trailing };
}

/**
 * Compare two decimal strings exactly (no float rounding): -1, 0 or 1, or
 * null when either is not a decimal string. "1e2" equals "100".
 */
export function compareDecimals(a: string, b: string): -1 | 0 | 1 | null {
  const x = decimalParts(a);
  const y = decimalParts(b);
  if (!x || !y) return null;
  const sign = (p: DecimalParts) => (p.digits === '' ? 0 : p.negative ? -1 : 1);
  const sx = sign(x);
  const sy = sign(y);
  if (sx !== sy) return sx < sy ? -1 : 1;
  if (sx === 0) return 0;
  // Same sign: compare magnitudes by the position of the leading digit,
  // then digit by digit.
  const adjX = x.exponent + BigInt(x.digits.length);
  const adjY = y.exponent + BigInt(y.digits.length);
  let magnitude: -1 | 0 | 1;
  if (adjX !== adjY) {
    magnitude = adjX < adjY ? -1 : 1;
  } else {
    const width = Math.max(x.digits.length, y.digits.length);
    const dx = x.digits.padEnd(width, '0');
    const dy = y.digits.padEnd(width, '0');
    magnitude = dx === dy ? 0 : dx < dy ? -1 : 1;
  }
  return (sx < 0 ? -magnitude : magnitude) as -1 | 0 | 1;
}

/**
 * The digits the server's canonical rendering of a decimal string needs
 * (schema.py `_coerce_number` without a precision: plain notation, integer
 * digits plus fractional digits, no leading or trailing zeros), or null
 * when it is not a decimal string. Zero needs 0.
 */
export function decimalDigitCount(value: string): number | null {
  const parts = decimalParts(value);
  if (!parts) return null;
  if (parts.digits === '') return 0;
  const length = BigInt(parts.digits.length);
  const exp = parts.exponent;
  let count: bigint;
  if (exp >= 0n) {
    count = length + exp;
  } else {
    const intDigits = length + exp > 0n ? length + exp : 0n;
    count = intDigits - exp;
  }
  return count > BigInt(Number.MAX_SAFE_INTEGER) ? Number.MAX_SAFE_INTEGER : Number(count);
}

/** A decimal string the server accepts as a number operand (<= 38 digits). */
function numberProblem(value: unknown): 'nan' | 'digits' | null {
  if (typeof value !== 'string' || !isDecimalString(value)) return 'nan';
  return (decimalDigitCount(value) ?? 0) > NUMBER_MAX_DIGITS ? 'digits' : null;
}

const TOO_MANY_DIGITS = `Use at most ${NUMBER_MAX_DIGITS} digits`;

// Python str.isspace / Cc / Cf / unpaired surrogates (schema.py
// `_coerce_url` and `_text_problem` with controls=False); U+2028 / U+2029
// are \s.
const URL_BAD_CHAR_RE = /[\s\\\p{Cc}\p{Cf}\p{Cs}]/u;
const URL_HEAD_RE = /^https?:\/\/([^/?#]*)/i;

/**
 * True when the server would accept `raw` as a url-column `eq` / `ne` /
 * `in` operand (schema.py `_coerce_url`): trimmed, non-empty, at most 2048
 * characters, no whitespace, control, format or backslash characters,
 * `http://` or `https://` with a non-empty host and a valid port.
 */
export function isHttpUrl(raw: string): boolean {
  const url = raw.trim();
  if (url === '' || [...url].length > URL_MAX_CHARS) return false;
  if (URL_BAD_CHAR_RE.test(url)) return false;
  const head = URL_HEAD_RE.exec(url);
  if (!head) return false;
  const authority = head[1];
  const hostPort = authority.slice(authority.lastIndexOf('@') + 1);
  if (authority.includes('[') !== authority.includes(']')) return false;
  let host: string;
  let port: string;
  if (hostPort.startsWith('[')) {
    const end = hostPort.indexOf(']');
    host = hostPort.slice(1, end);
    if (!/^[0-9a-f:.]+$/i.test(host) && !/^v[0-9a-f]+\..+$/i.test(host)) return false;
    port = hostPort.slice(end + 1).replace(/^[^:]*:?/, '');
  } else {
    const colon = hostPort.indexOf(':');
    host = colon < 0 ? hostPort : hostPort.slice(0, colon);
    port = colon < 0 ? '' : hostPort.slice(colon + 1);
  }
  if (host === '') return false;
  if (port !== '' && (!/^[0-9]+$/.test(port) || Number(port) > 65535)) return false;
  return true;
}

const URL_PROBLEM = 'Enter an http(s) URL';

/**
 * Split a one-per-line list entry. Lines that are empty or only whitespace
 * are dropped; other lines are kept exactly (text values are compared as
 * typed), except that with `numbers` each is trimmed (the server trims
 * number strings). Only newlines separate values: "1,234" stays one value
 * (and is refused as not a decimal).
 */
export function splitLines(raw: string, numbers = false): string[] {
  return raw
    .split('\n')
    .map((part) => (numbers ? part.trim() : part))
    .filter((part) => part.trim() !== '');
}

let idCounter = 0;

/** A fresh condition id. */
export function newConditionId(): string {
  idCounter += 1;
  return `fc${idCounter}`;
}

/**
 * Find a column by name: exact, else the one case-insensitive match (as the
 * server). The server folds with Python `casefold`, this with
 * `toLowerCase`; they differ only for rare Unicode names (e.g. "ß" vs
 * "SS"), which a hand-made link may then name in a form that reads as
 * missing here.
 */
export function findColumn(schema: TableColumn[], name: string): TableColumn | null {
  const exact = schema.find((column) => column.name === name);
  if (exact) return exact;
  const folded = name.toLowerCase();
  return schema.find((column) => column.name.toLowerCase() === folded) ?? null;
}

const isStringList = (value: unknown): value is string[] =>
  Array.isArray(value) && value.every((item) => typeof item === 'string');

/**
 * Why `condition` cannot be applied against `schema`, or null when it can.
 * Messages are short, for the form ("Enter a number").
 */
export function conditionError(condition: FilterCondition, schema: TableColumn[]): string | null {
  const column = findColumn(schema, condition.column);
  if (!column) return 'Choose a column';
  if (!opsForColumn(column).some((choice) => choice.op === condition.op)) {
    return 'Choose a condition';
  }
  const value = condition.value;
  const options = column.options ?? [];
  switch (valueKind(column, condition.op)) {
    case 'none':
      return null;
    case 'text':
      if (typeof value !== 'string' || value === '') return 'Enter a value';
      if (column.type === 'url' && condition.op !== 'contains' && condition.op !== 'starts_with'
        && condition.op !== 'ends_with' && !isHttpUrl(value)) {
        return URL_PROBLEM;
      }
      return null;
    case 'number': {
      const issue = numberProblem(value);
      return issue === 'nan' ? 'Enter a number' : issue === 'digits' ? TOO_MANY_DIGITS : null;
    }
    case 'pair': {
      if (!isStringList(value) || value.length !== 2) return 'Enter both bounds';
      const [lo, hi] = value;
      if (lo.trim() === '' || hi.trim() === '') return 'Enter both bounds';
      const issues = [numberProblem(lo), numberProblem(hi)];
      if (issues.includes('nan')) return 'Enter numbers';
      if (issues.includes('digits')) return TOO_MANY_DIGITS;
      return compareDecimals(lo, hi)! <= 0 ? null : 'The first bound must not be larger';
    }
    case 'boolean':
      return typeof value === 'boolean' ? null : 'Choose true or false';
    case 'booleans':
      return Array.isArray(value) && value.length > 0 && value.every((v) => typeof v === 'boolean')
        ? null
        : 'Choose at least one';
    case 'option':
      return typeof value === 'string' && options.includes(value) ? null : 'Choose an option';
    case 'options': {
      if (!isStringList(value) || value.length === 0) return 'Choose at least one option';
      if (!value.every((item) => options.includes(item))) return 'Unknown option';
      // One exact option set (multi-enum eq / ne) holds up to 200 options;
      // every list op (in, contains_any, contains_all) at most 100.
      const exactSet = column.multiple && (condition.op === 'eq' || condition.op === 'ne');
      const cap = exactSet ? FILTER_ENUM_MAX_OPTIONS : FILTER_IN_MAX_VALUES;
      return value.length <= cap ? null : `At most ${cap} options`;
    }
    case 'lines': {
      if (!isStringList(value) || value.length === 0) return 'Enter at least one value';
      if (value.some((item) => item.trim() === '')) return 'Remove the empty value';
      if (value.length > FILTER_IN_MAX_VALUES) return `At most ${FILTER_IN_MAX_VALUES} values`;
      if (column.type === 'number') {
        const issues = value.map(numberProblem);
        if (issues.includes('nan')) return 'Enter numbers only';
        if (issues.includes('digits')) return TOO_MANY_DIGITS;
      }
      if (column.type === 'url' && !value.every(isHttpUrl)) return URL_PROBLEM;
      return null;
    }
  }
}

/** True when `condition` is complete and valid against `schema`. */
export function isConditionComplete(condition: FilterCondition, schema: TableColumn[]): boolean {
  return conditionError(condition, schema) === null;
}

/** Why the condition list cannot be applied (the first problem), or null. */
export function conditionsError(conditions: FilterCondition[], schema: TableColumn[]): string | null {
  if (conditions.length > FILTER_MAX_CONDITIONS) {
    return `At most ${FILTER_MAX_CONDITIONS} filters`;
  }
  for (const condition of conditions) {
    const error = conditionError(condition, schema);
    if (error) return error;
  }
  return null;
}

function dedupe<T>(items: T[]): T[] {
  return items.filter((item, index) => items.indexOf(item) === index);
}

/**
 * One condition as a DSL leaf, in canonical form (numbers trimmed decimal
 * strings, option lists in schema order without repeats). The condition
 * should be complete (`isConditionComplete`); the column name is the
 * schema's spelling when the column is known.
 */
export function conditionToLeaf(condition: FilterCondition, schema: TableColumn[]): FilterNode {
  const column = findColumn(schema, condition.column);
  const name = column?.name ?? condition.column;
  if (!column) return { column: name, op: condition.op, value: condition.value };
  const value = condition.value;
  const options = column.options ?? [];
  switch (valueKind(column, condition.op)) {
    case 'none':
      return { column: name, op: condition.op };
    case 'number':
      return { column: name, op: condition.op, value: String(value).trim() };
    case 'pair': {
      const [lo, hi] = value as string[];
      return { column: name, op: condition.op, value: [lo.trim(), hi.trim()] };
    }
    case 'options':
      return {
        column: name,
        op: condition.op,
        value: options.filter((option) => (value as string[]).includes(option)),
      };
    case 'booleans':
      return { column: name, op: condition.op, value: dedupe(value as boolean[]) };
    case 'lines': {
      // Numbers are trimmed (the server trims them too); text and url
      // values go as typed, so a re-emitted chip never changes.
      const items = (value as string[]).map((item) => (column.type === 'number' ? item.trim() : item));
      return { column: name, op: condition.op, value: dedupe(items) };
    }
    default:
      return { column: name, op: condition.op, value };
  }
}

/** The builder state as a filter: null (none), the bare leaf (one), else `and`. */
export function conditionsToFilter(
  conditions: FilterCondition[],
  schema: TableColumn[],
): FilterNode | null {
  if (conditions.length === 0) return null;
  const leaves = conditions.map((condition) => conditionToLeaf(condition, schema));
  return leaves.length === 1 ? leaves[0] : { and: leaves };
}

/** What a filter is to the bar. */
export type FilterParse =
  | { kind: 'none' }
  | { kind: 'conditions'; conditions: FilterCondition[] }
  | { kind: 'custom'; reason: string };

function leafToCondition(node: unknown, schema: TableColumn[]): FilterCondition | string {
  if (!node || typeof node !== 'object' || Array.isArray(node)) return 'not a condition';
  const rec = node as Record<string, unknown>;
  const keys = Object.keys(rec);
  if (keys.some((key) => key !== 'column' && key !== 'op' && key !== 'value')) {
    return 'uses and / or / not';
  }
  if (typeof rec.column !== 'string' || typeof rec.op !== 'string') return 'not a condition';
  const column = findColumn(schema, rec.column);
  if (!column) return `no column named ${rec.column}`;
  const op = rec.op as FilterOp;
  if (!opsForColumn(column).some((choice) => choice.op === op)) return `op ${op} is not offered`;

  const raw = rec.value;
  let value: FilterValue;
  // A JSON number operand is read as its JS string form. The UI always
  // emits decimal strings; a hand-made link with a JSON number of more than
  // 15 significant digits has already lost precision in JSON.parse (the
  // server would have read it exactly), so such links should quote it.
  const asDecimal = (item: unknown): string | null =>
    typeof item === 'string' ? item : typeof item === 'number' && Number.isFinite(item) ? String(item) : null;
  switch (valueKind(column, op)) {
    case 'none':
      if (raw !== undefined && raw !== null) return 'unexpected value';
      value = null;
      break;
    case 'number': {
      const text = asDecimal(raw);
      if (text === null) return 'bad value';
      value = text;
      break;
    }
    case 'pair': {
      if (!Array.isArray(raw) || raw.length !== 2) return 'bad value';
      const lo = asDecimal(raw[0]);
      const hi = asDecimal(raw[1]);
      if (lo === null || hi === null) return 'bad value';
      value = [lo, hi];
      break;
    }
    case 'lines': {
      if (!Array.isArray(raw)) return 'bad value';
      const items = raw.map((item) => (column.type === 'number' ? asDecimal(item) : item));
      if (!isStringList(items)) return 'bad value';
      // One value per textarea line: a value spanning lines cannot be shown.
      if (items.some((item) => /[\r\n]/.test(item))) return 'a value spans lines';
      value = items;
      break;
    }
    case 'booleans':
      if (!Array.isArray(raw) || !raw.every((item) => typeof item === 'boolean')) return 'bad value';
      value = raw as boolean[];
      break;
    case 'options':
      if (!isStringList(raw)) return 'bad value';
      value = raw;
      break;
    case 'boolean':
      if (typeof raw !== 'boolean') return 'bad value';
      value = raw;
      break;
    default:
      if (typeof raw !== 'string') return 'bad value';
      value = raw;
  }
  const condition: FilterCondition = { id: newConditionId(), column: column.name, op, value };
  const error = conditionError(condition, schema);
  return error ? `invalid value (${error})` : condition;
}

/**
 * A filter as builder conditions: `none` for null, `conditions` for a leaf
 * or a flat `and` of leaves the bar can show, else `custom` with a reason.
 */
export function filterToConditions(node: FilterNode | null, schema: TableColumn[]): FilterParse {
  if (node === null) return { kind: 'none' };
  if (!node || typeof node !== 'object' || Array.isArray(node)) {
    return { kind: 'custom', reason: 'not a filter' };
  }
  const rec = node as Record<string, unknown>;
  let leaves: unknown[];
  if ('and' in rec && Object.keys(rec).length === 1) {
    if (!Array.isArray(rec.and) || rec.and.length === 0) return { kind: 'custom', reason: 'empty and' };
    leaves = rec.and;
  } else {
    leaves = [node];
  }
  if (leaves.length > FILTER_MAX_CONDITIONS) {
    return { kind: 'custom', reason: `more than ${FILTER_MAX_CONDITIONS} conditions` };
  }
  const conditions: FilterCondition[] = [];
  for (const leaf of leaves) {
    const result = leafToCondition(leaf, schema);
    if (typeof result === 'string') return { kind: 'custom', reason: result };
    conditions.push(result);
  }
  return { kind: 'conditions', conditions };
}

/** Every column name a filter's leaves reference (walks and / or / not). */
export function filterColumnNames(node: FilterNode): string[] {
  const out: string[] = [];
  const walk = (n: unknown, depth: number) => {
    if (!n || typeof n !== 'object' || depth > 16) return;
    const rec = n as Record<string, unknown>;
    if (Array.isArray(rec.and)) rec.and.forEach((child) => walk(child, depth + 1));
    else if (Array.isArray(rec.or)) rec.or.forEach((child) => walk(child, depth + 1));
    else if ('not' in rec) walk(rec.not, depth + 1);
    else if (typeof rec.column === 'string') out.push(rec.column);
  };
  walk(node, 0);
  return out;
}

/** The column names `node` references that `schema` lacks (dropped or renamed). */
export function missingFilterColumns(node: FilterNode | null, schema: TableColumn[]): string[] {
  if (!node) return [];
  const missing = filterColumnNames(node).filter((name) => !findColumn(schema, name));
  return missing.filter((name, index) => missing.indexOf(name) === index);
}

/** Chip text: "Status is Customer", "Revenue between 10 and 20", "Notes is empty". */
export function conditionLabel(condition: FilterCondition, schema: TableColumn[]): string {
  const column = findColumn(schema, condition.column);
  if (!column) return `${condition.column} ${condition.op}`;
  const head = `${column.name} ${opLabel(column, condition.op)}`;
  const value = condition.value;
  switch (valueKind(column, condition.op)) {
    case 'none':
      return head;
    case 'pair': {
      const [lo, hi] = value as string[];
      return `${head} ${lo.trim()} and ${hi.trim()}`;
    }
    case 'boolean':
      return `${head} ${String(value)}`;
    case 'booleans':
      return `${head} ${(value as boolean[]).map(String).join(', ')}`;
    case 'options':
    case 'lines':
      return `${head} ${(value as string[]).join(', ')}`;
    case 'number':
      return `${head} ${String(value).trim()}`;
    default:
      return `${head} ${String(value)}`;
  }
}

/**
 * Which chip a server filter error points at (`filterErrorPath`): the
 * condition index, `-1` for the filter as a whole (root, `and`, a bad JSON
 * `filter` param), or null when the error is not about the filter (sort,
 * paging, columns). `conditionCount` is how many chips the URL filter has;
 * with one chip the leaf is the root (paths `column`, `op`, `value...`).
 */
export function filterErrorTarget(path: string | null, conditionCount: number): number | null {
  if (path === null) return null;
  if (/^(sort|limit|offset|columns)\b/.test(path)) return null;
  const indexed = /^and\[(\d+)\]/.exec(path);
  if (indexed) {
    const index = Number(indexed[1]);
    return index < conditionCount ? index : -1;
  }
  if (conditionCount === 1 && /^(column|op|value)\b/.test(path)) return 0;
  return -1;
}

// --- URL params ---------------------------------------------------------------

/** The `filter` param value for a filter (its JSON), or null for none. */
export function filterToSearchParam(node: FilterNode | null): string | null {
  return node === null ? null : JSON.stringify(node);
}

/** A raw `filter` param as a filter node (any JSON object), else null. */
export function searchParamToFilter(raw: string | null): FilterNode | null {
  if (!raw) return null;
  try {
    const value: unknown = JSON.parse(raw);
    return value && typeof value === 'object' && !Array.isArray(value) ? (value as FilterNode) : null;
  } catch {
    return null;
  }
}

/** The `sort` param value for sort keys (`[{column, direction}]` JSON), or null for none. */
export function sortToSearchParam(keys: SortKey[] | null): string | null {
  return keys && keys.length > 0 ? JSON.stringify(keys) : null;
}

/**
 * A raw `sort` param as sort keys (a bare `{column, direction}` object is
 * accepted too; at most 3 keys; a missing or null `direction` is `asc`, as
 * the server reads it), else null when absent or malformed, including a
 * column named twice (the server refuses that; see the viewer for names
 * that differ only in case).
 */
export function searchParamToSort(raw: string | null): SortKey[] | null {
  if (!raw) return null;
  let value: unknown;
  try {
    value = JSON.parse(raw);
  } catch {
    return null;
  }
  const items = Array.isArray(value) ? value : [value];
  if (items.length === 0 || items.length > SORT_MAX_KEYS) return null;
  const keys: SortKey[] = [];
  for (const item of items) {
    if (!item || typeof item !== 'object') return null;
    const { column, direction } = item as { column?: unknown; direction?: unknown };
    if (typeof column !== 'string' || !column) return null;
    const dir = direction ?? 'asc';
    if (dir !== 'asc' && dir !== 'desc') return null;
    if (keys.some((key) => key.column === column)) return null;
    keys.push({ column, direction: dir });
  }
  return keys;
}
