import { describe, expect, it } from 'vitest';
import { boundedWordDiff, wordDiff } from './wordDiff';

const side = (before: string, after: string, drop: 'add' | 'del') =>
  wordDiff(before, after).filter((s) => s.type !== drop).map((s) => s.text).join('');

describe('wordDiff', () => {
  it('returns one equal segment for identical text', () => {
    expect(wordDiff('Drinks drip coffee', 'Drinks drip coffee'))
      .toEqual([{ type: 'equal', text: 'Drinks drip coffee' }]);
  });

  it('marks a replaced word, carrying its leading space', () => {
    expect(wordDiff('Drinks drip coffee', 'Drinks pour-over coffee')).toEqual([
      { type: 'equal', text: 'Drinks' },
      { type: 'del', text: ' drip' },
      { type: 'add', text: ' pour-over' },
      { type: 'equal', text: ' coffee' },
    ]);
  });

  it('keeps a rewritten phrase as one run instead of matching on spaces', () => {
    expect(wordDiff(
      'Works from the Berlin office (CET) and prefers mornings.',
      'Works remotely from Lisbon (WET) and prefers mornings.',
    )).toEqual([
      { type: 'equal', text: 'Works' },
      { type: 'add', text: ' remotely' },
      { type: 'equal', text: ' from' },
      { type: 'del', text: ' the Berlin office (CET)' },
      { type: 'add', text: ' Lisbon (WET)' },
      { type: 'equal', text: ' and prefers mornings.' },
    ]);
  });

  it('handles pure additions, pure removals and empty sides', () => {
    expect(wordDiff('', 'New')).toEqual([{ type: 'add', text: 'New' }]);
    expect(wordDiff('Old', '')).toEqual([{ type: 'del', text: 'Old' }]);
    expect(wordDiff('', '')).toEqual([]);
    expect(wordDiff('A b', 'A b c')).toEqual([
      { type: 'equal', text: 'A b' },
      { type: 'add', text: ' c' },
    ]);
  });

  it('round-trips both sides exactly, newlines and trailing space included', () => {
    const before = 'one\ntwo  three ';
    const after = 'one\nthree\nfour';
    expect(side(before, after, 'add')).toBe(before);
    expect(side(before, after, 'del')).toBe(after);
  });

  it('bounds the table for the differing middle only', () => {
    const words = Array.from({ length: 2000 }, (_, i) => `w${i}`).join(' ');
    // One changed word: the trimmed middle is a 1 x 1 table.
    expect(boundedWordDiff(words, words.replace('w1000', 'x'), 4)).toEqual([
      { type: 'equal', text: words.slice(0, words.indexOf(' w1000')) },
      { type: 'del', text: ' w1000' },
      { type: 'add', text: ' x' },
      { type: 'equal', text: words.slice(words.indexOf(' w1001')) },
    ]);
    expect(boundedWordDiff('a b c', 'x y z', 15)).toBeNull();
    expect(boundedWordDiff('a b c', 'x y z', 16)).toEqual(wordDiff('a b c', 'x y z'));
  });
});
