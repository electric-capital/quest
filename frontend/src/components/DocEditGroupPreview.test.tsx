// The write_doc multi_edit card body: one numbered block per edit with its
// location in the doc, the model's note and its own diff window -- plus
// the `doc_edit_group` branch of ActionRequestPreviewFields that mounts it
// in both card renderers.
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import type { DocEditGroupEntry, PreviewField, SkillContentDiff } from '../api/types';
import { ActionRequestPreviewFields, selectableChanges } from './ActionRequestPreviewFields';
import {
  DocEditGroupPreview,
  SkippedChangesLine,
  approveLabelFor,
  changeLocation,
  skippedItemsOf,
  skippedResultsOf,
} from './DocEditGroupPreview';

// Not under test here; keeps FileBrowser's import graph out of the run.
vi.mock('./SubagentReturnFilesPreview', () => ({
  SubagentReturnFilesPreview: () => null,
}));

function replaceLine(oldLine: number, from: string, to: string): SkillContentDiff {
  return {
    added: 1, removed: 1, total_old_lines: 20, total_new_lines: 20,
    lines: [
      { type: 'context', old_line: oldLine - 1, new_line: oldLine - 1, text: 'before' },
      { type: 'del', old_line: oldLine, new_line: null, text: from },
      { type: 'add', old_line: null, new_line: oldLine, text: to },
      { type: 'context', old_line: oldLine + 1, new_line: oldLine + 1, text: 'after' },
    ],
  };
}

const INSERT_AFTER_5: SkillContentDiff = {
  added: 1, removed: 0, total_old_lines: 20, total_new_lines: 21,
  lines: [
    { type: 'context', old_line: 5, new_line: 5, text: 'five' },
    { type: 'add', old_line: null, new_line: 6, text: 'new line' },
    { type: 'context', old_line: 6, new_line: 7, text: 'six' },
  ],
};

const EDITS: DocEditGroupEntry[] = [
  { index: 1, note: 'Fix the launch date', replace_all: false, replaced: 1,
    diff: replaceLine(12, 'Sept 4', 'Sept 18') },
  { index: 2, note: '', replace_all: true, replaced: 3,
    diff: replaceLine(3, 'Alice', 'Bob') },
];

const FIELD: PreviewField = {
  key: 'Changes',
  value: '2 changes, +2 / -2 line(s)',
  type: 'doc_edit_group',
  edits: EDITS,
};

afterEach(cleanup);

describe('changeLocation', () => {
  it('spans the removed lines', () => {
    expect(changeLocation(replaceLine(12, 'a', 'b'))).toBe('line 12');
    const two = replaceLine(12, 'a', 'b');
    two.lines.splice(2, 0, { type: 'del', old_line: 13, new_line: null, text: 'c' });
    expect(changeLocation(two)).toBe('lines 12–13');
  });

  it('names the line a pure insertion follows', () => {
    expect(changeLocation(INSERT_AFTER_5)).toBe('after line 5');
    expect(changeLocation({
      added: 1, removed: 0, lines: [{ type: 'add', old_line: null, new_line: 1, text: 'x' }],
    })).toBe('at the start');
  });
});

describe('DocEditGroupPreview', () => {
  it('renders one numbered block per edit with location, note and diff', () => {
    const { container } = render(<DocEditGroupPreview edits={EDITS} />);
    expect(screen.getByText('Change 1 of 2')).toBeTruthy();
    expect(screen.getByText('Change 2 of 2')).toBeTruthy();
    expect(screen.getByText('line 12')).toBeTruthy();
    expect(screen.getByText('line 3')).toBeTruthy();
    expect(screen.getByText('Fix the launch date')).toBeTruthy();
    expect(screen.getByText('replace all (3 matches)')).toBeTruthy();
    expect(container.querySelectorAll('.doc-edit-group-note')).toHaveLength(1);
    expect(container.querySelectorAll('.skill-diff-table')).toHaveLength(2);
    // The changed words are highlighted in their own spans.
    const added = container.querySelector('.skill-diff-line.add .skill-diff-text');
    expect(added?.textContent).toBe('Sept 18');
  });
});

describe('ActionRequestPreviewFields doc_edit_group branch', () => {
  it.each(['action-request-preview', 'request-preview'] as const)(
    'mounts the group under its label line (%s)', (classPrefix) => {
      const { container } = render(
        <ActionRequestPreviewFields
          previewFields={[FIELD]}
          params={{}}
          conversationId="c1"
          classPrefix={classPrefix}
        />,
      );
      const field = container.querySelector(`.${classPrefix}-field.doc-edit-group-field`);
      expect(field).toBeTruthy();
      expect(field?.querySelector(`.${classPrefix}-key`)?.textContent).toBe(
        'Changes: 2 changes, +2 / -2 line(s)',
      );
      expect(field?.querySelectorAll('.doc-edit-group-item')).toHaveLength(2);
    },
  );

  it('falls back to the plain value without the edits payload', () => {
    const { container } = render(
      <ActionRequestPreviewFields
        previewFields={[{ ...FIELD, edits: undefined }]}
        params={{}}
        conversationId="c1"
        classPrefix="action-request-preview"
      />,
    );
    expect(container.querySelector('.doc-edit-group')).toBeNull();
    expect(container.querySelector('.action-request-preview-value')?.textContent).toBe(
      '2 changes, +2 / -2 line(s)',
    );
  });
});

describe('partial approval', () => {
  it('renders read-only without a selection', () => {
    const { container } = render(<DocEditGroupPreview edits={EDITS} />);
    expect(container.querySelector('input[type="checkbox"]')).toBeNull();
    expect(container.querySelector('.doc-edit-group-reason')).toBeNull();
  });

  it('ticks every change, and an unticked one dims and takes a reason', () => {
    const onToggle = vi.fn();
    const onReason = vi.fn();
    const { container, rerender } = render(
      <DocEditGroupPreview
        edits={EDITS}
        selection={{ skipped: {}, onToggle, onReason }}
      />,
    );
    const boxes = container.querySelectorAll<HTMLInputElement>('input[type="checkbox"]');
    expect(boxes).toHaveLength(2);
    expect(boxes[0].checked && boxes[1].checked).toBe(true);
    expect(container.querySelector('.doc-edit-group-item.skipped')).toBeNull();

    // Unticking change 2 reports its 0-based position.
    fireEvent.click(boxes[1]);
    expect(onToggle).toHaveBeenCalledWith(1);

    rerender(
      <DocEditGroupPreview
        edits={EDITS}
        selection={{ skipped: { 1: 'keep Alice' }, onToggle, onReason }}
      />,
    );
    const items = container.querySelectorAll('.doc-edit-group-item');
    expect(items[0].classList.contains('skipped')).toBe(false);
    expect(items[1].classList.contains('skipped')).toBe(true);
    expect(items[1].querySelector<HTMLInputElement>('input[type="checkbox"]')?.checked).toBe(false);
    expect(items[1].querySelector('.doc-edit-group-skip-flag')?.textContent).toBe('skipped');
    const reason = items[1].querySelector<HTMLInputElement>('.doc-edit-group-reason');
    expect(reason?.value).toBe('keep Alice');
    expect(items[0].querySelector('.doc-edit-group-reason')).toBeNull();
    fireEvent.change(reason!, { target: { value: 'keep Alice as owner' } });
    expect(onReason).toHaveBeenCalledWith(1, 'keep Alice as owner');
  });

  it('reaches the group through the preview fields host', () => {
    const onToggle = vi.fn();
    const { container } = render(
      <ActionRequestPreviewFields
        previewFields={[FIELD]}
        params={{}}
        conversationId="c1"
        classPrefix="action-request-preview"
        editSelection={{ skipped: {}, onToggle, onReason: () => {} }}
      />,
    );
    fireEvent.click(container.querySelectorAll('input[type="checkbox"]')[0]);
    expect(onToggle).toHaveBeenCalledWith(0);
    expect(selectableChanges([FIELD])).toBe(2);
    expect(selectableChanges([{ key: 'Doc', value: 'Notes' }])).toBe(0);
    expect(selectableChanges(undefined)).toBe(0);
  });

  it('builds the resolve body from the selection, sorted by position', () => {
    expect(skippedItemsOf({ 3: 'later', 0: '' })).toEqual([
      { index: 0, reason: '' },
      { index: 3, reason: 'later' },
    ]);
  });

  it('labels the Apply button by the kept count and disables an empty selection', () => {
    expect(approveLabelFor('Apply', 5, 0)).toEqual({ label: 'Apply', disabled: false });
    expect(approveLabelFor('Apply', 5, 2)).toEqual({ label: 'Apply 3 of 5', disabled: false });
    expect(approveLabelFor('Apply', 5, 5).disabled).toBe(true);
    expect(approveLabelFor('Apply', 5, 5).title).toMatch(/Revise or Stop/);
    // A card without skippable changes keeps its label whatever is passed.
    expect(approveLabelFor('Send', 0, 0)).toEqual({ label: 'Send', disabled: false });
  });

  it('lists what an executed card left out', () => {
    expect(skippedResultsOf(null)).toEqual([]);
    expect(skippedResultsOf({ edits_applied: 3 })).toEqual([]);
    const skipped = skippedResultsOf({
      skipped: [
        { index: 1, note: 'Add the design owner', reason: 'Jordan left' },
        { index: 3, note: '', reason: '' },
        'junk',
      ],
    });
    expect(skipped).toHaveLength(2);
    const { container } = render(<SkippedChangesLine skipped={skipped} classPrefix="action-request" />);
    expect(container.querySelector('.action-request-feedback')?.textContent).toBe(
      'Skipped:change 2 (Add the design owner) — Jordan left; change 4',
    );
    expect(render(<SkippedChangesLine skipped={[]} classPrefix="request" />).container.innerHTML).toBe('');
  });
});
