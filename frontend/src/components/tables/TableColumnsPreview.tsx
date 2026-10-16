/**
 * The create_table approval card's column table (preview field type
 * `table_columns`, payload `field.columns`): one row per proposed column, in
 * order, with Name / Type / Required / Unique / Default / Options.
 *
 * - Name: the column name; a description shows as a muted second line
 *   (clipped by CSS, full text in `title`).
 * - Type: `columnTypeLabel()`: `enum[]` for a multi-select enum,
 *   `number (2 dp)` for a number with a precision, else the type.
 * - Required / Unique: "yes", blank when off.
 * - Default: `tableValueText()` (booleans true / false, a multi-enum list
 *   joined with ", "), cut by `previewText()` in the cell and its `title`;
 *   blank when there is none.
 * - Options: enum options as chips.
 *
 * Header cells carry `scope="col"`. The column dicts are key-less on the
 * card; a `key`, if present, is only a React key and never rendered. The
 * helpers here are shared by TableSchemaDiffPreview and
 * TableRowsDiffPreview.
 */
import type { TableSchemaDiffColumn } from '../../api/types';
import { truncateCellText } from '../../utils/tableCells';
import './TableColumnsPreview.css';

/** A column's type as shown in previews: `enum[]`, `number (2 dp)`, else the type. */
export function columnTypeLabel(
  column: Pick<TableSchemaDiffColumn, 'type' | 'multiple' | 'precision'>,
): string {
  if (column.type === 'enum' && column.multiple) return 'enum[]';
  if (column.type === 'number' && column.precision !== null && column.precision !== undefined) {
    return `number (${column.precision} dp)`;
  }
  return column.type;
}

/**
 * A logical table value (cell or column default) as display text, or null
 * when there is no value (null / undefined, or an empty multi-enum list, as
 * in the viewer grid). Booleans are "true" / "false", a multi-enum list is
 * joined with ", ", an empty string stays empty.
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
 * A value's text as previews put it in the DOM and in `title`: cut at the
 * viewer grid's TABLE_CELL_MAX_CHARS code points with an ellipsis
 * (`truncateCellText`), so a 10,000-character cell stays a short string.
 */
export function previewText(text: string): string {
  return truncateCellText(text).display;
}

export interface TableColumnsPreviewProps {
  columns: TableSchemaDiffColumn[];
}

export function TableColumnsPreview({ columns }: TableColumnsPreviewProps) {
  if (columns.length === 0) return null;
  return (
    <div className="table-columns-preview">
      <table className="table-columns-table">
        <thead>
          <tr>
            <th scope="col">Name</th>
            <th scope="col">Type</th>
            <th scope="col">Required</th>
            <th scope="col">Unique</th>
            <th scope="col">Default</th>
            <th scope="col">Options</th>
          </tr>
        </thead>
        <tbody>
          {columns.map((column, i) => {
            const defaultValue = tableValueText(column.default);
            const defaultText = defaultValue === null ? null : previewText(defaultValue);
            return (
              <tr key={column.key ?? `${i}:${column.name}`}>
                <td className="table-columns-name-cell">
                  <span className="table-columns-name">{column.name}</span>
                  {column.description && (
                    <span className="table-columns-desc" title={column.description}>
                      {column.description}
                    </span>
                  )}
                </td>
                <td className="table-columns-type">{columnTypeLabel(column)}</td>
                <td>{column.required ? 'yes' : ''}</td>
                <td>{column.unique ? 'yes' : ''}</td>
                <td className="table-columns-default" title={defaultText ?? undefined}>
                  {defaultText ?? ''}
                </td>
                <td>
                  {column.options && column.options.length > 0 && (
                    <span className="table-diff-chips">
                      {column.options.map((option) => (
                        <span key={option} className="table-diff-chip">{option}</span>
                      ))}
                    </span>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
