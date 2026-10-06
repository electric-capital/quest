import React, { useMemo, useState } from 'react';
import type { SkillContentDiff, SkillContentDiffLine } from '../api/types';
import './SkillContentDiffPreview.css';

interface SkillContentDiffPreviewProps {
  diff: SkillContentDiff;
}

/** Unchanged lines kept around each change in the collapsed snippet view. */
const CONTEXT_LINES = 2;

interface Hunk {
  start: number; // inclusive index into diff.lines
  end: number;   // inclusive
}

/**
 * Group the visible-when-collapsed line indexes (changed lines plus
 * CONTEXT_LINES of context on each side) into contiguous hunks.
 */
function computeHunks(lines: SkillContentDiffLine[]): Hunk[] {
  const visible = new Set<number>();
  lines.forEach((line, i) => {
    if (line.type === 'context') return;
    const from = Math.max(0, i - CONTEXT_LINES);
    const to = Math.min(lines.length - 1, i + CONTEXT_LINES);
    for (let j = from; j <= to; j++) visible.add(j);
  });
  const hunks: Hunk[] = [];
  let current: Hunk | null = null;
  for (let i = 0; i < lines.length; i++) {
    if (!visible.has(i)) {
      current = null;
      continue;
    }
    if (current && current.end === i - 1) {
      current.end = i;
    } else {
      current = { start: i, end: i };
      hunks.push(current);
    }
  }
  return hunks;
}

/**
 * Lines of the document above `lines[0]` that the rendered rows do not
 * show, from the 1-based line numbers: the old side when it has a line
 * (and something to report), else the new side (an add-only start).
 */
function countAbove(lines: SkillContentDiffLine[]): number {
  let oldCount = 0;
  let newCount = 0;
  const firstOld = lines.find((line) => line.old_line != null);
  if (firstOld?.old_line != null) oldCount = Math.max(0, firstOld.old_line - 1);
  const firstNew = lines.find((line) => line.new_line != null);
  if (firstNew?.new_line != null) newCount = Math.max(0, firstNew.new_line - 1);
  return oldCount > 0 ? oldCount : newCount;
}

/**
 * Lines of the document below the last of `lines` that the rendered rows
 * do not show, counted on the side of that last row: after an added line
 * (e.g. a truncated insertion, whose cut-off lines are additions) it is
 * `total_new_lines` minus its new-side number, otherwise `total_old_lines`
 * minus the last old-side number. The other side is the fallback when the
 * chosen side has no total (or no line number).
 */
function countBelow(
  lines: SkillContentDiffLine[],
  totalOld: number | undefined,
  totalNew: number | undefined,
): number {
  if (lines.length === 0) return 0;
  const below = (side: 'old_line' | 'new_line', total: number | undefined): number | null => {
    if (typeof total !== 'number') return null;
    for (let i = lines.length - 1; i >= 0; i--) {
      const n = lines[i][side];
      if (n != null) return Math.max(0, total - n);
    }
    return null;
  };
  const oldBelow = () => below('old_line', totalOld);
  const newBelow = () => below('new_line', totalNew);
  const [primary, fallback] = lines[lines.length - 1].type === 'add'
    ? [newBelow, oldBelow]
    : [oldBelow, newBelow];
  return primary() ?? fallback() ?? 0;
}

/**
 * Renders a content-diff approval card field: a unified line diff of a
 * skill body / routine prompt / Quest Doc (computed server-side at
 * proposal time). Collapsed by default to the changed hunks plus 2
 * context lines each side, with a toggle to expand (changed lines stay
 * highlighted).
 *
 * Two payload shapes:
 * - Whole-body (edit_skill / edit_routine): `lines` covers the entire
 *   old/new content, so the toggle expands to the full content.
 * - Bounded window (write_doc, flagged by `total_old_lines` /
 *   `total_new_lines`): `lines` is only the changed region plus 3 context
 *   lines, optionally cut at a line cap (`truncated`). The card then marks
 *   the document lines outside the rendered rows with "lines above / below
 *   not shown" separators, the toggle only reveals the window's context
 *   lines, and a truncation note follows the stats.
 */
export function SkillContentDiffPreview({ diff }: SkillContentDiffPreviewProps) {
  const [showFull, setShowFull] = useState(false);
  const hunks = useMemo(() => computeHunks(diff.lines), [diff.lines]);
  if (diff.lines.length === 0) return null;

  const bounded = typeof diff.total_old_lines === 'number'
    || typeof diff.total_new_lines === 'number';

  const renderLine = (line: SkillContentDiffLine, i: number) => (
    <tr key={i} className={`skill-diff-line ${line.type}`}>
      <td className="skill-diff-gutter">{line.old_line ?? ''}</td>
      <td className="skill-diff-gutter">{line.new_line ?? ''}</td>
      <td className="skill-diff-marker">
        {line.type === 'add' ? '+' : line.type === 'del' ? '-' : ''}
      </td>
      <td className="skill-diff-text">{line.text || ' '}</td>
    </tr>
  );

  const hiddenBetween = (prevEnd: number, nextStart: number) => {
    const count = nextStart - prevEnd - 1;
    if (count <= 0) return null;
    return (
      <tr key={`sep-${prevEnd}`} className="skill-diff-separator">
        <td colSpan={4}>&#8943; {count} unchanged line{count === 1 ? '' : 's'} &#8943;</td>
      </tr>
    );
  };

  // Bounded window only: document lines outside the rendered rows.
  const edgeSeparator = (key: string, count: number, where: 'above' | 'below') => (
    count > 0 ? (
      <tr key={key} className={`skill-diff-separator skill-diff-edge ${where}`}>
        <td colSpan={4}>
          &#8943; {count} line{count === 1 ? '' : 's'} {where} not shown &#8943;
        </td>
      </tr>
    ) : null
  );

  // Index range of diff.lines the rows render, and the rows themselves.
  let firstShown = 0;
  let lastShown = diff.lines.length - 1;
  let rows: React.ReactNode[];
  if (showFull || hunks.length === 0) {
    rows = diff.lines.map(renderLine);
  } else {
    rows = [];
    firstShown = hunks[0].start;
    lastShown = hunks[hunks.length - 1].end;
    let prevEnd = -1;
    for (const hunk of hunks) {
      // In a bounded window the leading / trailing collapsed context folds
      // into the "lines above / below not shown" edge separators instead.
      const sep = bounded && prevEnd === -1 ? null : hiddenBetween(prevEnd, hunk.start);
      if (sep) rows.push(sep);
      for (let i = hunk.start; i <= hunk.end; i++) {
        rows.push(renderLine(diff.lines[i], i));
      }
      prevEnd = hunk.end;
    }
    const tailSep = bounded ? null : hiddenBetween(prevEnd, diff.lines.length);
    if (tailSep) rows.push(tailSep);
  }

  if (bounded) {
    const above = edgeSeparator(
      'edge-above', countAbove(diff.lines.slice(firstShown)), 'above',
    );
    const below = edgeSeparator(
      'edge-below',
      countBelow(
        diff.lines.slice(0, lastShown + 1),
        diff.total_old_lines,
        diff.total_new_lines,
      ),
      'below',
    );
    rows = [above, ...rows, below].filter((row) => row != null);
  }

  return (
    <div className="skill-diff">
      <div className="skill-diff-scroll">
        <table className="skill-diff-table">
          <tbody>{rows}</tbody>
        </table>
      </div>
      <div className="skill-diff-footer">
        <span className="skill-diff-stats">
          <span className="skill-diff-stat-add">+{diff.added}</span>
          {' / '}
          <span className="skill-diff-stat-del">-{diff.removed}</span>
          {' line(s)'}
        </span>
        <button
          type="button"
          className="skill-diff-toggle"
          onClick={() => setShowFull((prev) => !prev)}
        >
          {/* Generic label: this preview also renders routine prompt diffs,
              not just skill bodies. A bounded window holds only the
              context around the changes, so "full content" would mislead. */}
          {showFull
            ? 'Show changes only'
            : bounded ? 'Show context lines' : 'Show full content'}
        </button>
      </div>
      {diff.truncated && (
        <div className="skill-diff-truncated-note">
          Diff truncated to the first {diff.lines.length} lines; the
          +{diff.added} / -{diff.removed} counts are exact.
        </div>
      )}
    </div>
  );
}
