import { describe, expect, it } from 'vitest';
import { isPublicProjectsEnabled, shouldShowDocModeBadge } from './docMode';

describe('shouldShowDocModeBadge', () => {
  it('hides the badge of a private doc while public projects are closed', () => {
    expect(shouldShowDocModeBadge('private', false)).toBe(false);
  });

  it('always shows the badge of a public doc, even with the gate closed', () => {
    expect(shouldShowDocModeBadge('public', false)).toBe(true);
  });

  it('shows both badges while public projects are open', () => {
    expect(shouldShowDocModeBadge('private', true)).toBe(true);
    expect(shouldShowDocModeBadge('public', true)).toBe(true);
  });
});

describe('isPublicProjectsEnabled', () => {
  it('reads the public_projects feature gate', () => {
    expect(isPublicProjectsEnabled(['docs', 'public_projects'])).toBe(true);
    expect(isPublicProjectsEnabled(['docs', 'public_project_routines'])).toBe(false);
    expect(isPublicProjectsEnabled([])).toBe(false);
  });
});
