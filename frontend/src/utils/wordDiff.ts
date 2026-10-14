/**
 * Word-level diff of two short texts, for inline "what changes" previews
 * (the suggested-memories panel shows how a suggestion rewrites the memory
 * it replaces). A token is a word with the whitespace before it attached
 * (plus any trailing whitespace run), so edits highlight at word
 * granularity and spacing alone never counts as a match -- with bare
 * whitespace tokens, the common subsequence latches onto the spaces and a
 * rewritten phrase comes out as alternating one-word changes.
 *
 * Plain longest-common-subsequence over the tokens: memory text is capped
 * at 4 KB, so the quadratic table is at most a few hundred thousand cells.
 */

export type WordDiffSegmentType = 'equal' | 'del' | 'add';

export interface WordDiffSegment {
  type: WordDiffSegmentType;
  text: string;
}

function tokenize(text: string): string[] {
  return text.match(/\s*\S+|\s+/g) ?? [];
}

/**
 * Segments of `before` turned into `after`, in reading order: `del` text
 * only exists in `before`, `add` text only in `after`, `equal` in both.
 * Adjacent segments of one type are merged, so each change is one span.
 * A change segment may start with the whitespace that preceded its first
 * word; renderers usually leave that part unhighlighted.
 */
export function wordDiff(before: string, after: string): WordDiffSegment[] {
  const a = tokenize(before);
  const b = tokenize(after);
  const n = a.length;
  const m = b.length;

  // lcs[i][j] = LCS length of a[i..] and b[j..], flattened.
  const width = m + 1;
  const lcs = new Uint32Array((n + 1) * width);
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      lcs[i * width + j] = a[i] === b[j]
        ? lcs[(i + 1) * width + j + 1] + 1
        : Math.max(lcs[(i + 1) * width + j], lcs[i * width + j + 1]);
    }
  }

  const segments: WordDiffSegment[] = [];
  const push = (type: WordDiffSegmentType, text: string) => {
    const last = segments[segments.length - 1];
    if (last && last.type === type) last.text += text;
    else segments.push({ type, text });
  };

  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    if (a[i] === b[j]) {
      push('equal', a[i]);
      i++;
      j++;
    } else if (lcs[(i + 1) * width + j] >= lcs[i * width + j + 1]) {
      push('del', a[i]);
      i++;
    } else {
      push('add', b[j]);
      j++;
    }
  }
  while (i < n) push('del', a[i++]);
  while (j < m) push('add', b[j++]);
  return segments;
}
