import type { ReactNode } from 'react';
import type { PreviewField } from '../api/types';
import { DocImagePreview } from './DocImagePreview';
import { SpreadsheetDiffPreview } from './SpreadsheetDiffPreview';
import { SkillContentDiffPreview } from './SkillContentDiffPreview';
import { SubagentReturnFilesPreview } from './SubagentReturnFilesPreview';
import { TableColumnsPreview } from './tables/TableColumnsPreview';
import { TableRowsDiffPreview } from './tables/TableRowsDiffPreview';
import { TableSchemaDiffPreview } from './tables/TableSchemaDiffPreview';
import './ActionRequestPreviewFields.css';

/**
 * Quest Tables field types -> their preview, or null when the payload key
 * is missing (the field then falls back to the plain value). All three
 * stack full width under the shared `table-preview-field` class
 * (ActionRequestPreviewFields.css).
 */
const TABLE_FIELD_PREVIEWS: Record<string, (field: PreviewField) => ReactNode | null> = {
  table_columns: (f) => (f.columns ? <TableColumnsPreview columns={f.columns} /> : null),
  table_schema_diff: (f) => (f.schema_diff ? <TableSchemaDiffPreview diff={f.schema_diff} /> : null),
  table_rows_diff: (f) => (f.rows_diff ? <TableRowsDiffPreview diff={f.rows_diff} /> : null),
};

function tableFieldPreview(field: PreviewField): ReactNode | null {
  const render = field.type ? TABLE_FIELD_PREVIEWS[field.type] : undefined;
  return render ? render(field) : null;
}

/**
 * The label line over a Quest Tables preview. The column table's value
 * ("5 columns: Title (text), ...") repeats the table below in the raw type
 * names, so `table_columns` shows its key with the count ("Columns (5)");
 * the schema diff and row grid keep "key: value", a useful summary.
 */
export function tableFieldLabel(field: PreviewField): string {
  if (field.type === 'table_columns' && field.columns) return `${field.key} (${field.columns.length})`;
  return `${field.key}: ${field.value}`;
}

/**
 * Structured action-request preview fields, shared by the chat card
 * (ActionRequestMessage) and the Requests view. `classPrefix` selects the
 * host's CSS namespace ('action-request-preview' or 'request-preview').
 * Unknown structured field types fall back to the plain value string, and
 * a missing preview_fields list falls back to raw params JSON (old chat
 * history). Structured fields stack full width (label line, then the
 * preview); a known type whose payload key is missing also falls back to
 * the plain value. Quest Tables types: `table_columns` (`field.columns`,
 * create_table), `table_schema_diff` (`field.schema_diff`,
 * change_table_schema), `table_rows_diff` (`field.rows_diff`,
 * write_table_rows); their label line is `tableFieldLabel()`.
 */
export function ActionRequestPreviewFields({
  previewFields,
  params,
  conversationId,
  classPrefix,
}: {
  previewFields?: PreviewField[];
  params: Record<string, unknown>;
  conversationId: string;
  classPrefix: 'action-request-preview' | 'request-preview';
}) {
  if (!previewFields || previewFields.length === 0) {
    return (
      <div className={classPrefix}>
        <pre>{JSON.stringify(params, null, 2)}</pre>
      </div>
    );
  }
  return (
    <div className={classPrefix}>
      {previewFields.map((field, i) => {
        const tablePreview = tableFieldPreview(field);
        return field.type === 'spreadsheet_diff' && field.grid ? (
          <div key={i} className={`${classPrefix}-field sheet-diff-field`}>
            <span className={`${classPrefix}-key`}>{field.key}: {field.value}</span>
            <SpreadsheetDiffPreview grid={field.grid} />
          </div>
        ) : field.type === 'skill_content_diff' && field.diff ? (
          <div key={i} className={`${classPrefix}-field skill-diff-field`}>
            <span className={`${classPrefix}-key`}>{field.key}: {field.value}</span>
            <SkillContentDiffPreview diff={field.diff} />
          </div>
        ) : field.type === 'subagent_return_files' && field.files ? (
          <div key={i} className={`${classPrefix}-field subagent-files-field`}>
            <span className={`${classPrefix}-key`}>{field.key}: {field.value}</span>
            <SubagentReturnFilesPreview files={field.files} conversationId={conversationId} />
          </div>
        ) : tablePreview ? (
          <div key={i} className={`${classPrefix}-field table-preview-field`}>
            <span className={`${classPrefix}-key`}>{tableFieldLabel(field)}</span>
            {tablePreview}
          </div>
        ) : field.type === 'doc_image' && field.image ? (
          <div key={i} className={`${classPrefix}-field doc-image-field`}>
            {/* The image caption already shows the name and size. */}
            <span className={`${classPrefix}-key`}>{field.key}:</span>
            <DocImagePreview
              image={field.image}
              conversationId={conversationId}
              // write_doc add_image: placement "none" stores the asset
              // without appending the markdown line.
              placed={params.placement !== 'none'}
            />
          </div>
        ) : (
          <div key={i} className={`${classPrefix}-field`}>
            <span className={`${classPrefix}-key`}>{field.key}:</span>
            <span className={`${classPrefix}-value`}>{field.value}</span>
          </div>
        );
      })}
    </div>
  );
}
