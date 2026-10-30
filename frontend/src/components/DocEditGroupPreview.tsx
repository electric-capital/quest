import type { DocEditGroupEntry, SkillContentDiff } from '../api/types';
import { SkillContentDiffPreview } from './SkillContentDiffPreview';
import './DocEditGroupPreview.css';

interface DocEditGroupPreviewProps {
  edits: DocEditGroupEntry[];
}

/**
 * Where in the doc an edit lands, from its diff's old-side line numbers:
 * the span of the removed lines ("line 12" / "lines 12-14"), or, for a
 * pure insertion, the line it follows ("after line 12"; "at the start"
 * when nothing precedes it).
 */
export function changeLocation(diff: SkillContentDiff): string {
  const removed = diff.lines
    .filter((line) => line.type === 'del' && line.old_line != null)
    .map((line) => line.old_line as number);
  if (removed.length > 0) {
    const first = Math.min(...removed);
    const last = Math.max(...removed);
    return first === last ? `line ${first}` : `lines ${first}–${last}`;
  }
  const firstAdd = diff.lines.findIndex((line) => line.type === 'add');
  for (let i = firstAdd - 1; i >= 0; i--) {
    const before = diff.lines[i].old_line;
    if (before != null) return `after line ${before}`;
  }
  return 'at the start';
}

/**
 * The body of a write_doc `multi_edit` approval card: one numbered block
 * per edit of the group, in the request's order, each with its position,
 * where it lands in the doc, the model's note as its heading and its own
 * bounded diff window (SkillContentDiffPreview, the write_doc shape). The
 * edits were matched against the same body, so every window's old-side
 * line numbers are the doc's as it is now.
 */
export function DocEditGroupPreview({ edits }: DocEditGroupPreviewProps) {
  return (
    <ol className="doc-edit-group">
      {edits.map((edit) => (
        <li key={edit.index} className="doc-edit-group-item">
          <div className="doc-edit-group-head">
            <span className="doc-edit-group-index">
              Change {edit.index} of {edits.length}
            </span>
            <span className="doc-edit-group-where">{changeLocation(edit.diff)}</span>
            {edit.replace_all && (
              <span className="doc-edit-group-flag">
                replace all ({edit.replaced} match{edit.replaced === 1 ? '' : 'es'})
              </span>
            )}
          </div>
          {edit.note && <div className="doc-edit-group-note">{edit.note}</div>}
          <SkillContentDiffPreview diff={edit.diff} />
        </li>
      ))}
    </ol>
  );
}
