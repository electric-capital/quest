/**
 * Word-level diff of two texts, for inline "what changes" previews (the
 * suggested-memories panel shows how a suggestion rewrites the memory it
 * replaces; content-diff cards highlight the changed words of a rewritten
 * line, see inlineDiff.ts). A token is a word with the whitespace before it
 * attached (plus any trailing whitespace run), so edits highlight at word
 * granularity and spacing alone never counts as a match -- with bare
 * whitespace tokens, the common subsequence latches onto the spaces and a
 * rewritten phrase comes out as alternating one-word changes.
 *
 * Plain longest-common-subsequence over the tokens left after trimming the
 * common prefix and suffix, so a small edit in a long paragraph costs a
 * small table. `wordDiff` is unbounded (memory text is capped at 4 KB);
 * `boundedWordDiff` refuses a table over a cell budget.
 */

export type WordDiffSegmentType = 'equal' | 'del' | 'add';

export interface WordDiffSegment {
  type: WordDiffSegmentType;
  text: string;
}

function tokenize(text: string): string[] {
  return text.match(/\s*\S+|\s+/g) ?? [];
}

function diffTokens(a: string[], b: string[], maxCells: number): WordDiffSegment[] | null {
  // A common prefix / suffix is part of some longest common subsequence.
  let start = 0;
  while (start < a.length && start < b.length && a[start] === b[start]) start++;
  let endA = a.length;
  let endB = b.length;
  while (endA > start && endB > start && a[endA - 1] === b[endB - 1]) {
    endA--;
    endB--;
  }
  const n = endA - start;
  const m = endB - start;
  if ((n + 1) * (m + 1) > maxCells) return null;

  // lcs[i][j] = LCS length of a[start+i..endA) and b[start+j..endB), flattened.
  const width = m + 1;
  const lcs = new Uint32Array((n + 1) * width);
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      lcs[i * width + j] = a[start + i] === b[start + j]
        ? lcs[(i + 1) * width + j + 1] + 1
        : Math.max(lcs[(i + 1) * width + j], lcs[i * width + j + 1]);
    }
  }

  const segments: WordDiffSegment[] = [];
  const push = (type: WordDiffSegmentType, text: string) => {
    if (!text) return;
    const last = segments[segments.length - 1];
    if (last && last.type === type) last.text += text;
    else segments.push({ type, text });
  };

  push('equal', a.slice(0, start).join(''));
  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    if (a[start + i] === b[start + j]) {
      push('equal', a[start + i]);
      i++;
      j++;
    } else if (lcs[(i + 1) * width + j] >= lcs[i * width + j + 1]) {
      push('del', a[start + i]);
      i++;
    } else {
      push('add', b[start + j]);
      j++;
    }
  }
  while (i < n) push('del', a[start + i++]);
  while (j < m) push('add', b[start + j++]);
  push('equal', a.slice(endA).join(''));
  return segments;
}

/**
 * Segments of `before` turned into `after`, in reading order: `del` text
 * only exists in `before`, `add` text only in `after`, `equal` in both.
 * Adjacent segments of one type are merged, so each change is one span.
 * A change segment may start with the whitespace that preceded its first
 * word; renderers usually leave that part unhighlighted.
 */
export function wordDiff(before: string, after: string): WordDiffSegment[] {
  return diffTokens(tokenize(before), tokenize(after), Infinity)!;
}

/**
 * `wordDiff`, or null when the table for the differing middle (after the
 * common prefix and suffix are trimmed) would exceed `maxCells` entries.
 */
export function boundedWordDiff(
  before: string,
  after: string,
  maxCells: number,
): WordDiffSegment[] | null {
  return diffTokens(tokenize(before), tokenize(after), maxCells);
}
