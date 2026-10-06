import { describe, expect, it } from 'vitest';
import { shouldShowDocModeBadge } from './docMode';

describe('shouldShowDocModeBadge', () => {
  it('shows the badge of a public doc', () => {
    expect(shouldShowDocModeBadge('public')).toBe(true);
  });

  it('never shows a badge on a private doc', () => {
    expect(shouldShowDocModeBadge('private')).toBe(false);
  });
});
