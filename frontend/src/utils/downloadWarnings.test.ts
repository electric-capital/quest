// The rule deciding which workspace downloads must be acknowledged first:
// inert text is silent, markup warns, code warns severely, and everything
// else -- including the unknown -- warns.
import { describe, expect, it } from 'vitest';
import { getDownloadWarning } from './downloadWarnings';

describe('getDownloadWarning', () => {
  it.each([
    'notes.txt', 'config.json', 'settings.yaml', 'app.toml', 'server.log', 'refs.bib',
  ])('lets inert text %s through silently', (name) => {
    expect(getDownloadWarning({ name, kind: 'file' })).toBeNull();
  });

  it.each(['README.md', 'guide.rst', 'feed.xml', 'styles.css', 'data.csv', 'rows.tsv'])(
    'warns for markup %s, whose rendering can fetch remote references', (name) => {
      const warning = getDownloadWarning({ name, kind: 'file' });
      expect(warning?.category).toBe('markup');
      expect(warning?.severity).toBe('warning');
      expect(warning?.detail).toMatch(/markdown image URL/);
    },
  );

  it.each(['script.py', 'query.sql', 'main.ts', 'bundle.js', 'run.sh', 'analysis.ipynb', 'fix.patch'])(
    'warns severely for code %s', (name) => {
      const warning = getDownloadWarning({ name, kind: 'file' });
      expect(warning?.category).toBe('code');
      expect(warning?.severity).toBe('severe');
    },
  );

  it('matches the extension case-insensitively', () => {
    expect(getDownloadWarning({ name: 'NOTES.TXT', kind: 'file' })).toBeNull();
    expect(getDownloadWarning({ name: 'Photo.JPG', kind: 'file' })?.category).toBe('image');
  });

  it.each([
    ['report.html', 'web'],
    ['chart.svg', 'web'],
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

  it('marks only code and executables as severe', () => {
    expect(getDownloadWarning({ name: 'setup.exe', kind: 'file' })?.severity).toBe('severe');
    expect(getDownloadWarning({ name: 'photo.jpg', kind: 'file' })?.severity).toBe('warning');
    expect(getDownloadWarning({ name: 'site.zip', kind: 'file' })?.severity).toBe('warning');
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
