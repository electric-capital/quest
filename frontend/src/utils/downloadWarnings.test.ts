// The rule deciding which workspace downloads must be acknowledged first:
// plain text is silent, everything else -- including the unknown -- warns.
import { describe, expect, it } from 'vitest';
import { getDownloadWarning } from './downloadWarnings';

describe('getDownloadWarning', () => {
  it.each([
    'notes.txt', 'README.md', 'data.csv', 'config.json', 'script.py', 'styles.css', 'query.sql', 'main.ts',
  ])('lets plain-text %s through silently', (name) => {
    expect(getDownloadWarning({ name, kind: 'file' })).toBeNull();
  });

  it('matches the extension case-insensitively', () => {
    expect(getDownloadWarning({ name: 'NOTES.TXT', kind: 'file' })).toBeNull();
    expect(getDownloadWarning({ name: 'Photo.JPG', kind: 'file' })?.category).toBe('image');
  });

  it.each([
    ['report.html', 'web'],
    ['chart.svg', 'web'],
    ['bundle.js', 'web'],
    ['photo.jpg', 'image'],
    ['diagram.png', 'image'],
    ['deck.pptx', 'document'],
    ['summary.pdf', 'document'],
    ['bundle.zip', 'archive'],
    ['site.tar.gz', 'archive'],
    ['clip.mp4', 'media'],
    ['setup.exe', 'executable'],
    ['run.bat', 'executable'],
  ] as const)('warns for %s as %s', (name, category) => {
    const warning = getDownloadWarning({ name, kind: 'file' });
    expect(warning?.category).toBe(category);
    expect(warning?.detail).toBeTruthy();
  });

  it('warns for unknown and missing extensions, since the agent picks the name', () => {
    expect(getDownloadWarning({ name: 'blob.qzx', kind: 'file' })?.category).toBe('other');
    expect(getDownloadWarning({ name: 'Makefile', kind: 'file' })?.category).toBe('other');
    // A leading dot is a hidden-file marker, not an extension.
    expect(getDownloadWarning({ name: '.env', kind: 'file' })?.category).toBe('other');
  });

  it('classifies by the last path segment', () => {
    expect(getDownloadWarning({ name: 'out.d/report.txt', kind: 'file' })).toBeNull();
    expect(getDownloadWarning({ name: 'notes.txt.d/report.png', kind: 'file' })?.category).toBe('image');
  });

  it('treats every folder download as an archive, whatever the folder is called', () => {
    expect(getDownloadWarning({ name: 'notes.txt', kind: 'folder' })?.category).toBe('archive');
    expect(getDownloadWarning({ name: 'reports', kind: 'folder' })?.category).toBe('archive');
  });
});
