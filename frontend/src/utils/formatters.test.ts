import { describe, expect, it } from 'vitest';
import { parseUTCTimestamp } from './formatters';

describe('parseUTCTimestamp', () => {
  const instant = Date.UTC(2026, 9, 1, 12, 0, 0);

  it('reads a naive timestamp as UTC', () => {
    expect(parseUTCTimestamp('2026-10-01T12:00:00').getTime()).toBe(instant);
    expect(parseUTCTimestamp('2026-10-01T12:00:00.000000').getTime()).toBe(instant);
  });

  it('keeps an explicit Z', () => {
    expect(parseUTCTimestamp('2026-10-01T12:00:00Z').getTime()).toBe(instant);
  });

  it('keeps an explicit offset (the table change log format)', () => {
    expect(parseUTCTimestamp('2026-10-01T12:00:00.000000+00:00').getTime()).toBe(instant);
    expect(parseUTCTimestamp('2026-10-01T14:00:00+02:00').getTime()).toBe(instant);
    expect(parseUTCTimestamp('2026-10-01T07:00:00-0500').getTime()).toBe(instant);
  });
});
