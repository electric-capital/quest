import { describe, expect, it } from 'vitest';
import type { AdminUsageBucket } from '../api/types';
import {
  describeMetricDelta,
  describeWindow,
  formatDateRange,
  formatPeriodLabel,
  formatPeriodTick,
  niceCeil,
  niceCeilEven,
  selectTickIndices,
} from './usageReport';
import { formatCompactNumber } from './formatters';

function point(start: string, end: string = start): AdminUsageBucket {
  return {
    start,
    end,
    conversations: 0,
    routine_conversations: 0,
    active_users: 0,
    new_users: 0,
    call_count: 0,
    total_tokens: 0,
    cost_usd: 0,
    cost_source: null,
    known_cost_usd: 0,
  };
}

describe('describeMetricDelta', () => {
  it('reports signed change with a percentage against a non-zero base', () => {
    const d = describeMetricDelta(14, 10, 'count', 'the 7 days before');
    expect(d.trend).toBe('up');
    expect(d.text).toBe('+4 (+40%)');
    expect(d.title).toBe('vs the 7 days before: 10');
  });

  it('formats cost deltas as dollars', () => {
    const d = describeMetricDelta(7.5, 10, 'cost', 'yesterday');
    expect(d.trend).toBe('down');
    expect(d.text).toBe('-$2.50 (-25%)');
    expect(d.title).toBe('vs yesterday: $10.00');
  });

  it('flags growth from a zero base instead of dividing by zero', () => {
    const d = describeMetricDelta(3, 0, 'count', 'yesterday');
    expect(d.trend).toBe('up');
    expect(d.text).toBe('+3 (from 0)');
  });

  it('treats sub-cent cost moves and equal counts as no change', () => {
    expect(describeMetricDelta(1.001, 1.0, 'cost', 'x').trend).toBe('flat');
    expect(describeMetricDelta(0, 0, 'count', 'x')).toMatchObject({ trend: 'flat', text: 'no change' });
  });

  it('yields n/a when either side is unpriced', () => {
    expect(describeMetricDelta(null, 2, 'cost', 'x').text).toBe('n/a');
    expect(describeMetricDelta(2, null, 'cost', 'x').trend).toBe('none');
  });
});

describe('period labels', () => {
  it('names the fixed windows', () => {
    expect(describeWindow(1)).toBe('Today');
    expect(describeWindow(30)).toBe('Last 30 days');
    expect(describeWindow(365)).toBe('Last 12 months');
  });

  it('formats daily, weekly and monthly points in UTC', () => {
    expect(formatPeriodLabel(point('2026-10-07'), 'daily')).toBe('Oct 7, 2026');
    expect(formatPeriodLabel(point('2026-10-05', '2026-10-11'), 'weekly')).toBe('Oct 5 – Oct 11, 2026');
    expect(formatPeriodLabel(point('2026-10-01', '2026-10-31'), 'monthly')).toBe('October 2026');
    expect(formatPeriodTick(point('2026-10-01', '2026-10-31'), 'monthly')).toBe("Oct '26");
    expect(formatPeriodTick(point('2026-01-01'), 'daily')).toBe('Jan 1');
  });

  it('collapses a one-day range and years the start when the range straddles a year', () => {
    expect(formatDateRange('2026-10-07', '2026-10-07')).toBe('Oct 7, 2026');
    expect(formatDateRange('2026-10-01', '2026-10-07')).toBe('Oct 1 – Oct 7, 2026');
    expect(formatDateRange('2025-10-07', '2026-10-06')).toBe('Oct 7, 2025 – Oct 6, 2026');
    expect(formatPeriodLabel(point('2025-12-29', '2026-01-04'), 'weekly')).toBe('Dec 29, 2025 – Jan 4, 2026');
  });
});

describe('axis math', () => {
  it('rounds maxima up to nice ceilings', () => {
    expect(niceCeil(0)).toBe(1);
    expect(niceCeil(7)).toBe(10);
    expect(niceCeil(12)).toBe(20);
    expect(niceCeil(23)).toBe(25);
    expect(niceCeil(40)).toBe(50);
    expect(niceCeil(2.5)).toBe(2.5);
    expect(niceCeil(1234567)).toBe(2000000);
  });

  it('uses even ceilings for integer series so the mid gridline is whole', () => {
    expect(niceCeilEven(0)).toBe(1);
    expect(niceCeilEven(1)).toBe(1);
    expect(niceCeilEven(3)).toBe(4);
    expect(niceCeilEven(5)).toBe(6);
    expect(niceCeilEven(7)).toBe(8);
    expect(niceCeilEven(9)).toBe(10);
    expect(niceCeilEven(31)).toBe(40);
    expect(niceCeilEven(250_000)).toBe(400_000);
  });

  it('spaces ticks evenly starting at the first point', () => {
    expect(selectTickIndices(90, 6)).toEqual([0, 15, 30, 45, 60, 75]);
    expect(selectTickIndices(5, 6)).toEqual([0, 1, 2, 3, 4]);
    expect(selectTickIndices(0, 6)).toEqual([]);
  });

  it('formats compact magnitudes', () => {
    expect(formatCompactNumber(950)).toBe('950');
    expect(formatCompactNumber(12345)).toBe('12.3K');
    expect(formatCompactNumber(4_000_000)).toBe('4M');
    expect(formatCompactNumber(1_250_000_000)).toBe('1.25B');
    expect(formatCompactNumber(250_000)).toBe('250K');
  });
});
