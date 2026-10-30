import type { DocEditGroupEntry, SkillContentDiff, SkippedItem, SkippedItemResult } from '../api/types';
import { SkillContentDiffPreview } from './SkillContentDiffPreview';
import './DocEditGroupPreview.css';

/**
 * The partial-approval state of an open multi_edit card, owned by the card
 * (ActionRequestMessage / RequestsView): the unticked changes by their
 * 0-based position with the reason typed so far. Absent on a resolved card
 * and in the expanded details of one, which render read-only.
 */
export interface DocEditSelection {
  skipped: Record<number, string>;
  onToggle: (position: number) => void;
  onReason: (position: number, reason: string) => void;
}

interface DocEditGroupPreviewProps {
  edits: DocEditGroupEntry[];
  selection?: DocEditSelection;
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

/** The `skipped_items` of the resolve body for a selection. */
export function skippedItemsOf(skipped: Record<number, string>): SkippedItem[] {
  return Object.keys(skipped)
    .map(Number)
    .sort((a, b) => a - b)
    .map((index) => ({ index, reason: skipped[index] }));
}

/**
 * The Approve button of a card with `total` skippable changes: the
 * handler's label ("Apply") as long as every change is ticked, "Apply 4 of
 * 5" with some unticked, and disabled with a hint when none is left
 * (rejecting everything is Revise or Stop).
 */
export function approveLabelFor(
  label: string,
  total: number,
  skippedCount: number,
): { label: string; disabled: boolean; title?: string } {
  if (total <= 0 || skippedCount <= 0) return { label, disabled: false };
  if (skippedCount >= total) {
    return {
      label,
      disabled: true,
      title: 'Every change is unticked: use Revise or Stop to reject them all.',
    };
  }
  return { label: `${label} ${total - skippedCount} of ${total}`, disabled: false };
}

/** The executed card's `result.skipped`, or [] when it has none. */
export function skippedResultsOf(result: unknown): SkippedItemResult[] {
  if (!result || typeof result !== 'object') return [];
  const skipped = (result as Record<string, unknown>).skipped;
  if (!Array.isArray(skipped)) return [];
  return skipped.filter(
    (item): item is SkippedItemResult =>
      !!item && typeof item === 'object' && typeof (item as SkippedItemResult).index === 'number',
  );
}

/**
 * The line under an executed multi_edit card that applied only some of
 * its changes: which were left out, with the user's reasons.
 */
export function SkippedChangesLine({
  skipped,
  classPrefix,
}: {
  skipped: SkippedItemResult[];
  classPrefix: 'action-request' | 'request';
}) {
  if (skipped.length === 0) return null;
  return (
    <div className={`${classPrefix}-feedback doc-edit-group-skipped`}>
      <span className={`${classPrefix}-feedback-label`}>Skipped:</span>
      {skipped.map((item, i) => (
        <span key={item.index} className="doc-edit-group-skipped-item">
          {i > 0 ? '; ' : ''}
          change {item.index + 1}
          {item.note ? ` (${item.note})` : ''}
          {item.reason ? ` — ${item.reason}` : ''}
        </span>
      ))}
    </div>
  );
}

/**
 * The body of a write_doc `multi_edit` approval card: one numbered block
 * per edit of the group, in the request's order, each with its position,
 * where it lands in the doc, the model's note as its heading and its own
 * bounded diff window (SkillContentDiffPreview, the write_doc shape). The
 * edits were matched against the same body, so every window's old-side
 * line numbers are the doc's as it is now. With a `selection` (the card is
 * open) each block has an Apply checkbox; an unticked block dims its diff
 * and takes an optional reason that goes to the model with the result.
 */
export function DocEditGroupPreview({ edits, selection }: DocEditGroupPreviewProps) {
  return (
    <ol className="doc-edit-group">
      {edits.map((edit) => {
        const position = edit.index - 1;
        const skipped = selection ? position in selection.skipped : false;
        const checkboxId = `doc-edit-apply-${edit.index}`;
        return (
          <li
            key={edit.index}
            className={`doc-edit-group-item${skipped ? ' skipped' : ''}`}
          >
            <div className="doc-edit-group-head">
              {selection && (
                <label className="doc-edit-group-apply" htmlFor={checkboxId}>
                  <input
                    id={checkboxId}
                    type="checkbox"
                    checked={!skipped}
                    onChange={() => selection.onToggle(position)}
                    aria-label={`Apply change ${edit.index} of ${edits.length}`}
                  />
                </label>
              )}
              <span className="doc-edit-group-index">
                Change {edit.index} of {edits.length}
              </span>
              <span className="doc-edit-group-where">{changeLocation(edit.diff)}</span>
              {edit.replace_all && (
                <span className="doc-edit-group-flag">
                  replace all ({edit.replaced} match{edit.replaced === 1 ? '' : 'es'})
                </span>
              )}
              {skipped && <span className="doc-edit-group-skip-flag">skipped</span>}
            </div>
            {edit.note && <div className="doc-edit-group-note">{edit.note}</div>}
            <SkillContentDiffPreview diff={edit.diff} />
            {selection && skipped && (
              <input
                type="text"
                className="doc-edit-group-reason"
                value={selection.skipped[position]}
                onChange={(e) => selection.onReason(position, e.target.value)}
                placeholder="Why skip this one? (optional, the AI reads it)"
                maxLength={500}
                aria-label={`Reason for skipping change ${edit.index}`}
              />
            )}
          </li>
        );
      })}
    </ol>
  );
}
