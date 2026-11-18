// Space-qualified workspace references (`chat://`, `proj://`,
// `autoproject://`): recognition and normalisation. Resolution against a
// space id is MarkdownLink / MarkdownImage's (MarkdownImage.test.tsx).
import { describe, expect, it } from 'vitest';
import { isWorkspaceRef, parseWorkspaceRef } from './workspaceRefs';

describe('isWorkspaceRef', () => {
  it.each([
    ['chat://out.csv', true],
    ['proj://reports/q3.md', true],
    ['autoproject://notes.md', true],
    ['AUTOPROJECT://Notes.md', true],
    ['autoproject://', true],
    ['https://example.com/a', false],
    ['autoproject:/notes.md', false],
    ['project://notes.md', false],
    ['task://notes.md', false],
    ['notes.md', false],
    ['javascript:alert(1)', false],
    [' autoproject://x', false],
  ])('%s -> %s', (url, expected) => {
    expect(isWorkspaceRef(url)).toBe(expected);
  });
});

describe('parseWorkspaceRef', () => {
  it.each([
    ['autoproject://reports/q3.md', 'autoproject', 'reports/q3.md'],
    ['autoproject:///reports/q3.md', 'autoproject', 'reports/q3.md'],
    ['autoproject://./reports/q3.md', 'autoproject', 'reports/q3.md'],
    ['autoproject://reports/q3%20final.md', 'autoproject', 'reports/q3 final.md'],
    ['Chat://Out.CSV', 'chat', 'Out.CSV'],
    ['proj://a/b/c.txt', 'proj', 'a/b/c.txt'],
    ['autoproject://', 'autoproject', ''],
    // A malformed escape keeps the raw path for the server to judge.
    ['chat://bad%zz.txt', 'chat', 'bad%zz.txt'],
    // Path checks are the server's: `..` is passed through, not refused here.
    ['proj://../x', 'proj', '../x'],
  ])('%s', (url, scheme, path) => {
    expect(parseWorkspaceRef(url)).toEqual({ scheme, path });
  });

  it.each(['https://x/y', 'notes.md', 'autoproject:notes.md', ''])('refuses %j', (url) => {
    expect(parseWorkspaceRef(url)).toBeNull();
  });
});
