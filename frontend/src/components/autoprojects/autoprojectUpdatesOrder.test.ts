// The Updates sort preference: "Recent first" by default, one stored choice
// for every AutoProject, and blocked storage never breaks the page.
import { afterEach, describe, expect, it, vi } from 'vitest';
import { readAutoProjectUpdatesOrder, writeAutoProjectUpdatesOrder } from './autoprojectUpdatesOrder';

const KEY = 'quest_autoproject_updates_order';

afterEach(() => {
  vi.restoreAllMocks();
  localStorage.clear();
});

describe('AutoProject updates order', () => {
  it('defaults to recent first', () => {
    expect(readAutoProjectUpdatesOrder()).toBe('recent');
  });

  it('a stored preference wins, and is written for every AutoProject', () => {
    writeAutoProjectUpdatesOrder('oldest');
    expect(localStorage.getItem(KEY)).toBe('oldest');
    expect(readAutoProjectUpdatesOrder()).toBe('oldest');
    writeAutoProjectUpdatesOrder('recent');
    expect(readAutoProjectUpdatesOrder()).toBe('recent');
  });

  it('reads an unknown stored value as the default', () => {
    localStorage.setItem(KEY, 'sideways');
    expect(readAutoProjectUpdatesOrder()).toBe('recent');
  });

  it('falls back to the default when storage throws', () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('SecurityError');
    });
    expect(readAutoProjectUpdatesOrder()).toBe('recent');
  });

  it('swallows a failing write', () => {
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('QuotaExceededError');
    });
    expect(() => writeAutoProjectUpdatesOrder('oldest')).not.toThrow();
  });
});
