// Length as the server's caps count it: Python `len(text.strip())`.
import { describe, expect, it } from 'vitest';
import { strippedCodePointLength } from './textLength';

describe('strippedCodePointLength', () => {
  it.each([
    ['', 0],
    ['   \n\t ', 0],
    ['abc', 3],
    ['  abc \n\n', 3],
    ['a b', 3],
    // One code point each, though two UTF-16 units.
    ['😀', 1],
    ['😀😀x', 3],
    ['\n\n# Brief\n\n', 7],
  ])('%j -> %i', (text, expected) => {
    expect(strippedCodePointLength(text)).toBe(expected);
  });
});
