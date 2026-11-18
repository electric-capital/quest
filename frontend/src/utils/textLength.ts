/**
 * Length of a text as the server's length caps count it (Python
 * `len(text.strip())`): code points of the trimmed text, so an emoji counts
 * once and leading or trailing blank lines do not count.
 */
export function strippedCodePointLength(text: string): number {
  return [...text.trim()].length;
}
