import { afterEach, describe, expect, it, vi } from 'vitest';
import type { Doc } from '../api/types';
import {
  SIDEBAR_DOC_LIMIT,
  deriveSidebarDocItems,
  docCountLabel,
  formatSidebarDocTime,
  formatSidebarDocTimeTitle,
} from './sidebarDocs';

function doc(overrides: Partial<Doc> & { id: string }): Doc {
  return {
    owner_id: 1,
    project_id: null,
    title: overrides.id,
    description: '',
    mode: 'private',
    content_size: 0,
    asset_count: 0,
    last_write_source: null,
    created_at: '2026-01-01T00:00:00',
    updated_at: '2026-01-01T00:00:00',
    scope: 'user',
    shared: false,
    access: { can_rename: true, can_switch_mode: false, can_delete: true, write: 'free' },
    ...overrides,
  };
}

describe('deriveSidebarDocItems', () => {
  it('orders newest updated_at first', () => {
    const rows = [
      doc({ id: 'old', updated_at: '2026-01-01T00:00:00' }),
      doc({ id: 'new', updated_at: '2026-01-03T00:00:00' }),
      doc({ id: 'mid', updated_at: '2026-01-02T12:30:00' }),
    ];
    expect(deriveSidebarDocItems(rows).map((d) => d.id)).toEqual(['new', 'mid', 'old']);
  });

  it('orders sub-millisecond differences by the raw timestamp', () => {
    const rows = [
      doc({ id: 'a', updated_at: '2026-01-01T00:00:00.000100' }),
      doc({ id: 'b', updated_at: '2026-01-01T00:00:00.000900' }),
      doc({ id: 'c', updated_at: '2026-01-01T00:00:00' }),
    ];
    expect(deriveSidebarDocItems(rows).map((d) => d.id)).toEqual(['b', 'a', 'c']);
  });

  it('breaks updated_at ties by id descending', () => {
    const same = '2026-01-02T00:00:00';
    const rows = [
      doc({ id: 'doc-a', updated_at: same }),
      doc({ id: 'doc-c', updated_at: same }),
      doc({ id: 'doc-b', updated_at: same }),
      doc({ id: 'doc-z', updated_at: '2026-01-01T00:00:00' }),
    ];
    expect(deriveSidebarDocItems(rows).map((d) => d.id)).toEqual(['doc-c', 'doc-b', 'doc-a', 'doc-z']);
  });

  it('keeps at most `limit` rows, defaulting to five', () => {
    const rows = Array.from({ length: 8 }, (_, i) =>
      doc({ id: `d${i}`, updated_at: `2026-01-0${i + 1}T00:00:00` }),
    );
    expect(SIDEBAR_DOC_LIMIT).toBe(5);
    expect(deriveSidebarDocItems(rows).map((d) => d.id)).toEqual(['d7', 'd6', 'd5', 'd4', 'd3']);
    expect(deriveSidebarDocItems(rows, 2).map((d) => d.id)).toEqual(['d7', 'd6']);
    expect(deriveSidebarDocItems(rows, 0)).toEqual([]);
    expect(deriveSidebarDocItems([], 5)).toEqual([]);
  });

  it('does not mutate its input', () => {
    const rows = [
      doc({ id: 'old', updated_at: '2026-01-01T00:00:00' }),
      doc({ id: 'new', updated_at: '2026-01-02T00:00:00' }),
    ];
    deriveSidebarDocItems(rows);
    expect(rows.map((d) => d.id)).toEqual(['old', 'new']);
  });
});

describe('docCountLabel', () => {
  it('shows the loaded count', () => {
    expect(docCountLabel(0, false)).toBe('0');
    expect(docCountLabel(3, false)).toBe('3');
    expect(docCountLabel(5, false)).toBe('5');
  });

  it('appends "+" when the server has more', () => {
    expect(docCountLabel(5, true)).toBe('5+');
  });
});

describe('formatSidebarDocTime', () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it('uses the relative label within a week', () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-10-06T12:00:00Z'));
    expect(formatSidebarDocTime('2026-10-06T11:55:00')).toBe('5m ago');
    expect(formatSidebarDocTime('2026-10-06T09:00:00')).toBe('3h ago');
    expect(formatSidebarDocTime('2026-10-03T12:00:00')).toBe('3d ago');
  });

  it('uses a short date (no time of day) for older docs', () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-10-06T12:00:00Z'));
    const sameYear = new Date('2026-08-15T12:00:00Z');
    expect(formatSidebarDocTime('2026-08-15T12:00:00')).toBe(
      sameYear.toLocaleDateString(undefined, { month: 'short', day: 'numeric' }),
    );
    const lastYear = new Date('2025-08-15T12:00:00Z');
    expect(formatSidebarDocTime('2025-08-15T12:00:00')).toBe(
      lastYear.toLocaleDateString(undefined, { month: 'short', year: 'numeric' }),
    );
  });

  it('returns empty strings for an unparseable timestamp', () => {
    expect(formatSidebarDocTime('not a date')).toBe('');
    expect(formatSidebarDocTimeTitle('not a date')).toBe('');
  });

  it('titles the time with the absolute timestamp', () => {
    expect(formatSidebarDocTimeTitle('2026-08-15T12:00:00')).toBe(
      `Updated ${new Date('2026-08-15T12:00:00Z').toLocaleString()}`,
    );
  });
});
