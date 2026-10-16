/**
 * Typed cell renderers of the Quest Tables grid (components/tables/TableGrid).
 *
 * Cells arrive in logical form (api/types.ts TableCellValue): text / url /
 * number as strings (numbers are canonical decimal strings: no exponent, no
 * trailing fractional zeros, "0" for every zero), booleans as JSON booleans,
 * a multi-enum as a list of options, null for no value. One renderer per
 * column type:
 *
 * - text: the string, cut at TABLE_CELL_MAX_CHARS code points with an
 *   ellipsis; the full value is in `title` whenever it was cut. The cell CSS
 *   also ellipsizes at the column's max width (TableGrid.css).
 * - number: right-aligned, at the column `precision` (formatDecimal); no
 *   precision shows the canonical string as stored. Pure string arithmetic,
 *   no Intl and no thousands separators: the grid shows exactly the digits
 *   the agent and the CSV download see, in every locale.
 * - url: a link (`target="_blank" rel="noopener noreferrer"`) showing the
 *   host, the full URL in `title`. A value that does not parse as an
 *   http(s) URL renders as text instead (never an href).
 * - boolean: a checkbox glyph, with "true" / "false" as visually hidden text
 *   for screen readers (`tableValueText`, the one boolean vocabulary).
 * - enum: single as one chip, multi as chips; an empty multi list is null.
 * - null: a muted italic "NULL", as in TableViewerModal.
 */

import { Square, SquareCheck } from 'lucide-react';
import type { TableCellValue, TableColumn, TableColumnType } from '../api/types';

/** Display cap for a text cell, in code points (the rest goes to `title`). */
export const TABLE_CELL_MAX_CHARS = 200;

/** User-facing names of the column types (the editor's type select). */
export const COLUMN_TYPE_LABELS: Record<TableColumnType, string> = {
  text: 'Text',
  number: 'Number',
  url: 'URL',
  boolean: 'Checkbox',
  enum: 'Choice',
};

/**
 * A logical table value (cell or column default) as display text, or null
 * when there is no value (null / undefined, or an empty multi-enum list, as
 * in the viewer grid). A multi-enum list is joined with ", ", an empty
 * string stays empty. Booleans are "true" / "false" on every Quest Tables
 * surface -- the grid's screen-reader text, the Columns panel (its default
 * select and read-only list), the card previews and History -- as in the
 * CSV download and the model's tools.
 */
export function tableValueText(value: unknown): string | null {
  if (value === null || value === undefined) return null;
  if (typeof value === 'string') return value;
  if (typeof value === 'boolean' || typeof value === 'number') return String(value);
  if (Array.isArray(value)) {
    if (value.length === 0) return null;
    return value.map((v) => tableValueText(v) ?? '').join(', ');
  }
  return JSON.stringify(value);
}

/**
 * A column's type in the one vocabulary every Quest Tables surface uses
 * (the Columns panel's summaries and read-only list, the create_table card's
 * column table, the schema diff of cards and History): the COLUMN_TYPE_LABELS
 * name, "Choice (several)" for a choice column that allows several options,
 * "Number (2 decimal places)" for a number with a precision. A type the
 * client does not know shows as sent.
 */
export function columnTypeLabel(column: {
  type: string;
  multiple?: boolean | null;
  precision?: number | null;
}): string {
  const label = COLUMN_TYPE_LABELS[column.type as TableColumnType] ?? column.type;
  if (column.type === 'enum' && column.multiple) return `${label} (several)`;
  if (column.type === 'number' && typeof column.precision === 'number') {
    return `${label} (${column.precision} ${column.precision === 1 ? 'decimal place' : 'decimal places'})`;
  }
  return label;
}

const DECIMAL_RE = /^(-?)(\d+)(?:\.(\d+))?$/;

/**
 * Format a canonical decimal string at `precision` decimals (0..10) with pure
 * string arithmetic: pads with zeros, or rounds half-even like the server's
 * own canonicalization (chat/tables/schema.py `_coerce_number`): '1.5' p=2
 * -> '1.50', '2.345' p=2 -> '2.34', '2.355' p=2 -> '2.36', '0.5' p=0 -> '0'.
 * A value that rounds to zero loses its sign ('-0.005' p=2 -> '0.00'), as
 * the server stores such a value as "0". `precision` null / undefined, or a
 * string that is not a plain decimal, comes back unchanged.
 */
export function formatDecimal(value: string, precision: number | null | undefined): string {
  if (precision === null || precision === undefined || precision < 0) return value;
  const match = DECIMAL_RE.exec(value);
  if (!match) return value;
  const [, sign, intPart, fracPart = ''] = match;
  let digits: string;
  if (fracPart.length <= precision) {
    digits = intPart + fracPart.padEnd(precision, '0');
  } else {
    const kept = intPart + fracPart.slice(0, precision);
    const rest = fracPart.slice(precision);
    const first = rest.charCodeAt(0) - 48;
    let roundUp: boolean;
    if (first > 5) roundUp = true;
    else if (first < 5) roundUp = false;
    else if (/[1-9]/.test(rest.slice(1))) roundUp = true;
    // Exactly half: to the even neighbour.
    else roundUp = (kept.charCodeAt(kept.length - 1) - 48) % 2 === 1;
    digits = roundUp ? incrementDigits(kept) : kept;
  }
  const intLen = digits.length - precision;
  let whole = digits.slice(0, intLen).replace(/^0+(?=\d)/, '');
  if (whole === '') whole = '0';
  const frac = digits.slice(intLen);
  const isZero = !/[1-9]/.test(digits);
  const body = precision > 0 ? `${whole}.${frac}` : whole;
  return sign && !isZero ? `-${body}` : body;
}

/** Add one to a string of decimal digits ('199' -> '200', '99' -> '100'). */
function incrementDigits(digits: string): string {
  const out = digits.split('');
  for (let i = out.length - 1; i >= 0; i -= 1) {
    if (out[i] === '9') {
      out[i] = '0';
    } else {
      out[i] = String.fromCharCode(out[i].charCodeAt(0) + 1);
      return out.join('');
    }
  }
  return `1${out.join('')}`;
}

/**
 * Cut `text` at `max` code points (never inside a surrogate pair), adding
 * an ellipsis; `truncated` says whether anything was cut.
 */
export function truncateCellText(
  text: string,
  max: number = TABLE_CELL_MAX_CHARS,
): { display: string; truncated: boolean } {
  if (text.length <= max) return { display: text, truncated: false };
  const points = Array.from(text);
  if (points.length <= max) return { display: text, truncated: false };
  return { display: `${points.slice(0, max).join('')}…`, truncated: true };
}

/** The host of an http(s) URL, or null when it does not parse as one. */
export function urlHost(value: string): string | null {
  try {
    const url = new URL(value);
    if (url.protocol !== 'http:' && url.protocol !== 'https:') return null;
    return url.host || null;
  } catch {
    return null;
  }
}

/** True when the cell has no value: null / missing, or an empty multi-enum. */
export function isNullCell(value: TableCellValue | number | undefined): boolean {
  return value === null || value === undefined || (Array.isArray(value) && value.length === 0);
}

/**
 * One grid cell as a <td>, rendered for its column type. `value` is the row
 * entry under `column.key`.
 */
export function TableCell({
  column,
  value,
}: {
  column: TableColumn;
  value: TableCellValue | number | undefined;
}) {
  if (isNullCell(value)) {
    return <td className="table-cell table-cell--null">NULL</td>;
  }
  switch (column.type) {
    case 'number': {
      const text = formatDecimal(String(value), column.precision);
      // Long values can hit the cell's max-width ellipsis: keep them readable.
      return (
        <td className="table-cell table-cell--number" title={text.length > 20 ? text : undefined}>
          {text}
        </td>
      );
    }
    case 'boolean': {
      const checked = value === true;
      return (
        <td className="table-cell table-cell--boolean">
          {checked ? (
            <SquareCheck size={16} className="table-cell-check table-cell-check--on" aria-hidden="true" />
          ) : (
            <Square size={16} className="table-cell-check" aria-hidden="true" />
          )}
          <span className="table-cell-sr-only">{tableValueText(checked)}</span>
        </td>
      );
    }
    case 'url': {
      const url = String(value);
      const host = urlHost(url);
      if (host === null) return <TextCell text={url} />;
      return (
        <td className="table-cell table-cell--url">
          <a href={url} target="_blank" rel="noopener noreferrer" title={url} className="table-cell-link">
            {host}
          </a>
        </td>
      );
    }
    case 'enum': {
      const options = Array.isArray(value) ? value : [String(value)];
      return (
        <td className="table-cell table-cell--enum" title={options.join(', ')}>
          <span className="table-cell-chips">
            {options.map((option, index) => (
              <span key={`${index}:${option}`} className="table-cell-chip">
                {option}
              </span>
            ))}
          </span>
        </td>
      );
    }
    default:
      return <TextCell text={Array.isArray(value) ? value.join(', ') : String(value)} />;
  }
}

function TextCell({ text }: { text: string }) {
  const { display, truncated } = truncateCellText(text);
  // The CSS ellipsis can hide part of even a short value: always offer the
  // whole text on hover when it is long enough to be cut by the column.
  const title = truncated || text.length > 40 || text.includes('\n') ? text : undefined;
  return (
    <td className="table-cell table-cell--text" title={title}>
      {display}
    </td>
  );
}
