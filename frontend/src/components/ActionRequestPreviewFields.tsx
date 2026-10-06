import type { PreviewField } from '../api/types';
import { DocImagePreview } from './DocImagePreview';
import { SpreadsheetDiffPreview } from './SpreadsheetDiffPreview';
import { SkillContentDiffPreview } from './SkillContentDiffPreview';
import { SubagentReturnFilesPreview } from './SubagentReturnFilesPreview';

/**
 * Structured action-request preview fields, shared by the chat card
 * (ActionRequestMessage) and the Requests view. `classPrefix` selects the
 * host's CSS namespace ('action-request-preview' or 'request-preview').
 * Unknown structured field types fall back to the plain value string, and
 * a missing preview_fields list falls back to raw params JSON (old chat
 * history).
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
      {previewFields.map((field, i) => (
        field.type === 'spreadsheet_diff' && field.grid ? (
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
        )
      ))}
    </div>
  );
}
