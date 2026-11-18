// Who wrote a brief: the attribution line, the History rows and the
// editor's "updated while you were editing" phrase, from the stored source
// (`run:<id>`, `ui`, `restore:<version>`) and the writing run's seq.
import { describe, expect, it } from 'vitest';
import { briefAttribution, briefWriterPhrase, parseBriefSource } from './autoprojectBriefSource';

describe('parseBriefSource', () => {
  it.each([
    ['ui', { kind: 'ui' }],
    ['run:4b2e', { kind: 'run' }],
    ['restore:12', { kind: 'restore', version: 12 }],
    ['restore:', { kind: 'unknown' }],
    ['restore:x', { kind: 'unknown' }],
    ['other', { kind: 'unknown' }],
    [null, { kind: 'unknown' }],
  ])('%j', (source, expected) => {
    expect(parseBriefSource(source)).toEqual(expected);
  });
});

describe('briefAttribution', () => {
  it.each([
    ['run:r1', 3, 'Update 3'],
    ['run:r1', null, 'An update'],
    ['ui', null, 'Edited by you'],
    ['ui', 7, 'Edited by you'],
    ['restore:5', null, 'Restored from version 5'],
    [null, null, null],
    ['bogus', 2, null],
  ])('%j / %j -> %j', (source, seq, expected) => {
    expect(briefAttribution(source, seq)).toBe(expected);
  });
});

describe('briefWriterPhrase', () => {
  it.each([
    ['run:r1', 4, 'by update 4'],
    ['run:r1', null, 'by an update'],
    ['restore:2', null, 'by a restore'],
    ['ui', null, 'elsewhere'],
    [null, null, 'elsewhere'],
  ])('%j / %j -> %j', (source, seq, expected) => {
    expect(briefWriterPhrase(source, seq)).toBe(expected);
  });
});
