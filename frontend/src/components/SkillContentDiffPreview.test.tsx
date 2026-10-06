// The content-diff card field in its two payload shapes: the whole-body
// diff edit_skill / edit_routine cards send (must render exactly as
// before), and write_doc's bounded window (changed region + 3 context
// lines, `total_*_lines`, optional `truncated`) with "lines above / below
// not shown" edge separators.
import { afterEach, describe, expect, it } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import type { SkillContentDiff, SkillContentDiffLine } from '../api/types';
import { SkillContentDiffPreview } from './SkillContentDiffPreview';

function context(oldLine: number, newLine: number = oldLine): SkillContentDiffLine {
  return { type: 'context', old_line: oldLine, new_line: newLine, text: `line ${oldLine}` };
}

function del(oldLine: number, text: string): SkillContentDiffLine {
  return { type: 'del', old_line: oldLine, new_line: null, text };
}

function add(newLine: number, text: string): SkillContentDiffLine {
  return { type: 'add', old_line: null, new_line: newLine, text };
}

const notShown = () => screen.queryAllByText(/not shown/);

describe('SkillContentDiffPreview', () => {
  afterEach(() => {
    cleanup();
  });

  it('renders a whole-body (legacy) diff unchanged: unchanged-line separators, no edge separators', () => {
    // 10-line skill body, line 5 replaced. No total_* fields.
    const diff: SkillContentDiff = {
      added: 1,
      removed: 1,
      lines: [
        context(1), context(2), context(3), context(4),
        del(5, 'old five'), add(5, 'new five'),
        context(6), context(7), context(8), context(9), context(10),
      ],
    };
    render(<SkillContentDiffPreview diff={diff} />);

    expect(screen.getByText('old five')).toBeTruthy();
    expect(screen.getByText('new five')).toBeTruthy();
    // Collapsed: 2 context lines each side, the rest folded.
    expect(screen.getByText(/2 unchanged lines/)).toBeTruthy();
    expect(screen.getByText(/3 unchanged lines/)).toBeTruthy();
    expect(screen.queryByText('line 1')).toBeNull();
    expect(notShown()).toHaveLength(0);
    expect(screen.queryByText(/Diff truncated/)).toBeNull();

    const toggle = screen.getByRole('button', { name: 'Show full content' });
    fireEvent.click(toggle);
    expect(screen.getByText('line 1')).toBeTruthy();
    expect(screen.getByText('line 10')).toBeTruthy();
    expect(screen.queryByText(/unchanged line/)).toBeNull();
    expect(notShown()).toHaveLength(0);
    expect(screen.getByRole('button', { name: 'Show changes only' })).toBeTruthy();
  });

  it('marks the document lines outside a bounded window as not shown', () => {
    // 100-line doc, line 43 replaced; the server window is lines 40..46.
    const diff: SkillContentDiff = {
      added: 1,
      removed: 1,
      lines: [
        context(40), context(41), context(42),
        del(43, 'old 43'), add(43, 'new 43'),
        context(44), context(45), context(46),
      ],
      truncated: false,
      total_old_lines: 100,
      total_new_lines: 100,
    };
    render(<SkillContentDiffPreview diff={diff} />);

    // Collapsed (2 context lines): line 40 and 46 fold into the edge
    // separators rather than getting their own "unchanged" rows.
    expect(screen.getByText(/^\S+ 40 lines above not shown \S+$/)).toBeTruthy();
    expect(screen.getByText(/^\S+ 55 lines below not shown \S+$/)).toBeTruthy();
    expect(screen.queryByText(/unchanged line/)).toBeNull();
    expect(screen.queryByText('line 40')).toBeNull();
    expect(screen.queryByText(/Diff truncated/)).toBeNull();

    fireEvent.click(screen.getByRole('button', { name: 'Show context lines' }));
    // Whole window: counts from the first / last emitted line.
    expect(screen.getByText('line 40')).toBeTruthy();
    expect(screen.getByText('line 46')).toBeTruthy();
    expect(screen.getByText(/39 lines above not shown/)).toBeTruthy();
    expect(screen.getByText(/54 lines below not shown/)).toBeTruthy();
    expect(notShown()).toHaveLength(2);
    expect(screen.getByRole('button', { name: 'Show changes only' })).toBeTruthy();
  });

  it('shows no edge separators when the window reaches both ends of the doc', () => {
    // 4-line doc, line 2 replaced: the window is the whole doc.
    const diff: SkillContentDiff = {
      added: 1,
      removed: 1,
      lines: [context(1), del(2, 'old 2'), add(2, 'new 2'), context(3), context(4)],
      truncated: false,
      total_old_lines: 4,
      total_new_lines: 4,
    };
    render(<SkillContentDiffPreview diff={diff} />);
    expect(notShown()).toHaveLength(0);
    expect(screen.getByRole('button', { name: 'Show context lines' })).toBeTruthy();
  });

  it('notes a truncated window and counts the cut-off additions below', () => {
    // 10-line doc + 1000 appended lines; the server cut the window at 400
    // lines (3 context + 397 additions).
    const lines: SkillContentDiffLine[] = [context(8), context(9), context(10)];
    for (let n = 11; n <= 407; n++) lines.push(add(n, `added ${n}`));
    const diff: SkillContentDiff = {
      added: 1000,
      removed: 0,
      lines,
      truncated: true,
      total_old_lines: 10,
      total_new_lines: 1010,
    };
    render(<SkillContentDiffPreview diff={diff} />);

    expect(
      screen.getByText('Diff truncated to the first 400 lines; the +1000 / -0 counts are exact.'),
    ).toBeTruthy();
    // The old side has nothing below line 10, so the new side reports.
    expect(screen.getByText(/603 lines below not shown/)).toBeTruthy();
    expect(screen.getByText(/8 lines above not shown/)).toBeTruthy();
  });

  it('counts below a truncated mid-doc insertion on the new side', () => {
    // 100-line doc, 1000 lines inserted after line 50; the server cut the
    // window at 400 lines (3 context + 397 additions). The old side still
    // has 50 lines below line 50, but the last rendered row is an addition,
    // so the count is the new doc's 1100 lines minus new line 447.
    const lines: SkillContentDiffLine[] = [context(48), context(49), context(50)];
    for (let n = 51; n <= 447; n++) lines.push(add(n, `inserted ${n}`));
    const diff: SkillContentDiff = {
      added: 1000,
      removed: 0,
      lines,
      truncated: true,
      total_old_lines: 100,
      total_new_lines: 1100,
    };
    render(<SkillContentDiffPreview diff={diff} />);

    expect(screen.getByText(/653 lines below not shown/)).toBeTruthy();
    expect(screen.queryByText(/50 lines below not shown/)).toBeNull();
    // Collapsed: line 48 folds into the edge separator above.
    expect(screen.getByText(/48 lines above not shown/)).toBeTruthy();
  });
});
