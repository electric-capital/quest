/**
 * The create_table approval card's column table (preview field type
 * `table_columns`, payload `field.columns`): one row per proposed column, in
 * order, with Name / Type / Required / Unique / Default / Options.
 *
 * - Name: the column name; a description shows as a muted second line
 *   (clipped by CSS, full text in `title`).
 * - Type: `columnTypeLabel()` (utils/tableCells), the editor's vocabulary:
 *   "Choice (several)", "Number (2 decimal places)", "Checkbox", ...
 * - Required / Unique: "yes", blank when off.
 * - Default: `tableValueText()` (utils/tableCells: booleans true / false, a
 *   multi-enum list joined with ", "), cut by `previewText()` in the cell
 *   and its `title`; blank when there is none.
 * - Options: enum options as chips.
 *
 * Header cells carry `scope="col"`. The column dicts are key-less on the
 * card; a `key`, if present, is only a React key and never rendered.
 * `previewText()` is shared by TableSchemaDiffPreview and
 * TableRowsDiffPreview.
 */
import type { TableSchemaDiffColumn } from '../../api/types';
import { columnTypeLabel, tableValueText, truncateCellText } from '../../utils/tableCells';
import './TableColumnsPreview.css';

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
                <td className="table-columns-options">
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
