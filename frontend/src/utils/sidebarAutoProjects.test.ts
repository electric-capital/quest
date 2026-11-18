// The sidebar AutoProjects block's rows: newest activity first, AutoProjects
// with no activity last, ties by id descending, at most five.
import { describe, expect, it } from 'vitest';
import type { AutoProjectListRow } from '../api/types';
import { deriveSidebarAutoProjectItems, SIDEBAR_AUTOPROJECT_LIMIT } from './sidebarAutoProjects';

function row(id: string, last_activity_at: string | null): AutoProjectListRow {
  return {
    id,
    title: id,
    archived: false,
    last_activity_at,
    active_run: null,
    unread: false,
    event_seq: 0,
    unread_seq: 0,
    read_seq: 0,
  };
}

const ids = (rows: AutoProjectListRow[]) => rows.map((r) => r.id);

describe('deriveSidebarAutoProjectItems', () => {
  it('orders by last activity, newest first, with no-activity rows last', () => {
    const rows = [
      row('a', '2026-11-01T10:00:00'),
      row('none', null),
      row('c', '2026-11-03T10:00:00'),
      row('bad', 'not a date'),
      row('b', '2026-11-02T10:00:00'),
    ];
    expect(ids(deriveSidebarAutoProjectItems(rows))).toEqual(['c', 'b', 'a', 'none', 'bad']);
  });

  it('orders by microseconds within one millisecond, then by id descending', () => {
    const rows = [
      row('x', '2026-11-01T10:00:00.000100'),
      row('y', '2026-11-01T10:00:00.000200'),
      row('a', '2026-11-01T09:00:00'),
      row('b', '2026-11-01T09:00:00'),
    ];
    expect(ids(deriveSidebarAutoProjectItems(rows))).toEqual(['y', 'x', 'b', 'a']);
  });

  it('lists at most five and never mutates its input', () => {
    const rows = Array.from({ length: 8 }, (_, i) => row(`r${i}`, `2026-11-0${i + 1}T00:00:00`));
    const before = ids(rows);
    const items = deriveSidebarAutoProjectItems(rows);
    expect(SIDEBAR_AUTOPROJECT_LIMIT).toBe(5);
    expect(ids(items)).toEqual(['r7', 'r6', 'r5', 'r4', 'r3']);
    expect(ids(rows)).toEqual(before);
    expect(deriveSidebarAutoProjectItems(rows, 0)).toEqual([]);
    expect(deriveSidebarAutoProjectItems(rows, -1)).toEqual([]);
  });
});
