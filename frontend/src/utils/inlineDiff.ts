/**
 * Word-level detail for the rows of a content line diff
 * (SkillContentDiffPreview). Quest Docs and skill bodies keep a whole
 * paragraph on one line, so a one-word edit shows as a paragraph removed
 * and re-added. This module:
 *
 * - pairs each removed line with the added line that replaced it (an
 *   order-keeping alignment of the removed / added run, scored by word
 *   similarity) and word-diffs the pair, so the renderer can highlight the
 *   changed words inside the two rows, GitHub-style. A pair that shares too
 *   little (a rewrite) stays two plain rows;
 * - shortens long unchanged text: in a long paired line, each stretch
 *   between or around the changes keeps up to KEEP_MAX characters next to
 *   the change, cut at a sentence end where one fits (else a word break);
 *   a long context line keeps its opening the same way. The rest is an
 *   `elided` piece the renderer folds behind an expander.
 *
 * Work is bounded: a pair's word diff gets at most MAX_PAIR_CELLS table
 * cells, a removed / added run is aligned by similarity only up to
 * MAX_ALIGN_CANDIDATES pairs (positionally beyond), and a diff computes at
 * most MAX_PAIR_DIFFS word diffs; lines past a bound render plain.
 */

import type { SkillContentDiffLine } from '../api/types';
import { boundedWordDiff, type WordDiffSegment } from './wordDiff';

export type InlinePieceType = 'equal' | 'change' | 'elided';

export interface InlinePiece {
  type: InlinePieceType;
  text: string;
}

export interface LineDetail {
  /**
   * The line's text in reading order (joined, exactly `line.text`): `change`
   * is a word the paired line does not have, `elided` unchanged text folded
   * by default. Null renders the line whole and unhighlighted.
   */
  pieces: InlinePiece[] | null;
  /** Index of the paired del / add line, so expanding one expands both. */
  partner: number | null;
}

/** Below this share of matching words a pair is a rewrite, not an edit. */
const MIN_SIMILARITY = 0.4;
const MAX_PAIR_CELLS = 250_000;
const MAX_ALIGN_CANDIDATES = 64;
const MAX_PAIR_DIFFS = 200;
/** Lines up to this long are never shortened. */
const SHORTEN_FROM = 240;
/** Most unchanged characters kept on one side of a change (or of a context line). */
const KEEP_MAX = 150;
/** Fewer hidden characters than this are not worth an expander. */
const ELIDE_MIN = 40;
/** An unchanged run of at most this many words between two changes joins them. */
const ABSORB_MAX_WORDS = 2;

const SENTENCE_END = /[.!?…]["'’”)\]]*(?=\s)/g;
const SENTENCE_BREAK = /[.!?…]["'’”)\]]*\s+/g;

/** Unchanged text and the changes between it; changes keep both sides. */
type Item = { kind: 'equal'; text: string } | { kind: 'change'; del: string; add: string };

/** A pair's pieces before they are split into the two rows. */
type PairPiece = { type: 'equal' | 'elided' | 'del' | 'add'; text: string };

function nonSpaceLength(text: string): number {
  return text.replace(/\s+/g, '').length;
}

/** Matching share of the pair's visible characters (2 * equal / both sides). */
function similarity(segments: WordDiffSegment[]): number {
  let equal = 0;
  let changed = 0;
  for (const segment of segments) {
    const length = nonSpaceLength(segment.text);
    if (segment.type === 'equal') equal += length;
    else changed += length;
  }
  const total = 2 * equal + changed;
  return total === 0 ? 1 : (2 * equal) / total;
}

function toItems(segments: WordDiffSegment[]): Item[] {
  const items: Item[] = [];
  for (const segment of segments) {
    const last = items[items.length - 1];
    if (segment.type === 'equal') {
      items.push({ kind: 'equal', text: segment.text });
    } else if (last?.kind === 'change') {
      last[segment.type] += segment.text;
    } else {
      items.push({
        kind: 'change',
        del: segment.type === 'del' ? segment.text : '',
        add: segment.type === 'add' ? segment.text : '',
      });
    }
  }
  return items;
}

/**
 * Fold a short unchanged run between two changes into them when it is no
 * longer than the change on either side (diff-match-patch's semantic
 * cleanup), so a rewritten phrase reads as one change instead of
 * alternating around a shared "to" or "the".
 */
function absorbShortEqualities(items: Item[]): Item[] {
  const out: Item[] = [];
  for (let k = 0; k < items.length; k++) {
    const item = items[k];
    const prev = out[out.length - 1];
    const next = items[k + 1];
    if (
      item.kind === 'equal'
      && prev?.kind === 'change'
      && next?.kind === 'change'
      && item.text.trim().split(/\s+/).length <= ABSORB_MAX_WORDS
      && item.text.length <= Math.max(prev.del.length, prev.add.length)
      && item.text.length <= Math.max(next.del.length, next.add.length)
    ) {
      prev.del += item.text + next.del;
      prev.add += item.text + next.add;
      k++;
      continue;
    }
    out.push(item.kind === 'change' ? { ...item } : item);
  }
  return out;
}

/**
 * Where the kept head of `text` ends: all of it within KEEP_MAX
 * characters, else the last sentence end, else the last word break, that
 * fits.
 */
function headCut(text: string): number {
  if (text.length <= KEEP_MAX) return text.length;
  let cut = -1;
  for (const match of text.matchAll(SENTENCE_END)) {
    const end = match.index + match[0].length;
    if (end > KEEP_MAX) break;
    cut = end;
  }
  if (cut > 0) return cut;
  const space = text.slice(0, KEEP_MAX + 1).search(/\s\S*$/);
  return space > 0 ? space : KEEP_MAX;
}

/**
 * Where the kept tail of `text` starts: all of it within KEEP_MAX
 * characters of the end, else the first sentence start, else the first
 * word break, that fits.
 */
function tailCut(text: string): number {
  if (text.length <= KEEP_MAX) return 0;
  const from = text.length - KEEP_MAX;
  for (const match of text.matchAll(SENTENCE_BREAK)) {
    const start = match.index + match[0].length;
    if (start >= from && start < text.length) return start;
  }
  const space = text.slice(from).search(/\s/);
  return space >= 0 ? from + space : from;
}

/**
 * Split an unchanged stretch into kept and elided pieces. `head` keeps its
 * start (text after a change, or a context line), `tail` its end (text
 * before a change), `both` its two ends (text between two changes).
 */
function shortenStretch(text: string, keep: 'head' | 'tail' | 'both'): PairPiece[] {
  let from = text.length;
  let to = text.length;
  if (keep === 'head') {
    from = headCut(text);
  } else if (keep === 'tail') {
    from = 0;
    to = tailCut(text);
  } else {
    from = headCut(text);
    to = tailCut(text);
  }
  if (to - from < ELIDE_MIN) return [{ type: 'equal', text }];
  return [
    { type: 'equal', text: text.slice(0, from) },
    { type: 'elided', text: text.slice(from, to) },
    { type: 'equal', text: text.slice(to) },
  ].filter((piece) => piece.text !== '') as PairPiece[];
}

function pairPieces(segments: WordDiffSegment[], shorten: boolean): PairPiece[] {
  const items = absorbShortEqualities(toItems(segments));
  const pieces: PairPiece[] = [];
  items.forEach((item, k) => {
    if (item.kind === 'change') {
      if (item.del) pieces.push({ type: 'del', text: item.del });
      if (item.add) pieces.push({ type: 'add', text: item.add });
      return;
    }
    // Items alternate, so an unchanged item has a change on each side it
    // has a neighbour on.
    const afterChange = k > 0;
    const beforeChange = k < items.length - 1;
    if (!shorten || (!afterChange && !beforeChange)) {
      pieces.push({ type: 'equal', text: item.text });
    } else {
      pieces.push(...shortenStretch(
        item.text,
        afterChange && beforeChange ? 'both' : afterChange ? 'head' : 'tail',
      ));
    }
  });
  return pieces;
}

/** One row's view of a pair: its own changes, without the other side's. */
function sidePieces(pieces: PairPiece[], side: 'del' | 'add'): InlinePiece[] {
  const out: InlinePiece[] = [];
  const push = (type: InlinePieceType, text: string) => {
    if (!text) return;
    const last = out[out.length - 1];
    if (last && last.type === type) last.text += text;
    else out.push({ type, text });
  };
  for (const piece of pieces) {
    if (piece.type === 'equal' || piece.type === 'elided') {
      push(piece.type, piece.text);
    } else if (piece.type === side) {
      // The whitespace before a changed word is spacing, not a change.
      const [, lead, rest] = /^(\s*)([\s\S]*)$/.exec(piece.text)!;
      push('equal', lead);
      push('change', rest);
    }
  }
  return out;
}

interface Budget {
  diffs: number;
}

/** The pair's word diff, or null when it is no edit worth highlighting. */
function diffPair(before: string, after: string, budget: Budget): WordDiffSegment[] | null {
  if (!before.trim() || !after.trim()) return null;
  if (budget.diffs >= MAX_PAIR_DIFFS) return null;
  budget.diffs++;
  const segments = boundedWordDiff(before, after, MAX_PAIR_CELLS);
  if (!segments || similarity(segments) < MIN_SIMILARITY) return null;
  return segments;
}

/**
 * Pair a removed run with the added run after it: the order-keeping
 * matching with the highest total similarity (or position by position
 * when the run is too big to score every pair).
 */
function alignRuns(
  lines: SkillContentDiffLine[],
  dels: number[],
  adds: number[],
  budget: Budget,
): Array<[number, number, WordDiffSegment[]]> {
  const n = dels.length;
  const m = adds.length;
  const pairs: Array<[number, number, WordDiffSegment[]]> = [];
  if (n * m > MAX_ALIGN_CANDIDATES) {
    for (let k = 0; k < Math.min(n, m); k++) {
      const segments = diffPair(lines[dels[k]].text, lines[adds[k]].text, budget);
      if (segments) pairs.push([dels[k], adds[k], segments]);
    }
    return pairs;
  }

  const candidates = dels.map((d) => adds.map((a) => diffPair(lines[d].text, lines[a].text, budget)));
  const scores = candidates.map((row) => row.map((segments) => (segments ? similarity(segments) : 0)));
  // best[i][j] = highest total similarity matching dels[i..] with adds[j..].
  const best = Array.from({ length: n + 1 }, () => new Float64Array(m + 1));
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      best[i][j] = Math.max(best[i + 1][j], best[i][j + 1], scores[i][j] + best[i + 1][j + 1]);
    }
  }
  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    const segments = candidates[i][j];
    if (segments && best[i][j] === scores[i][j] + best[i + 1][j + 1]) {
      pairs.push([dels[i], adds[j], segments]);
      i++;
      j++;
    } else if (best[i][j] === best[i + 1][j]) {
      i++;
    } else {
      j++;
    }
  }
  return pairs;
}

/** Per-line detail for `lines`, index for index. */
export function inlineLineDetails(lines: SkillContentDiffLine[]): LineDetail[] {
  const details: LineDetail[] = lines.map((line) => {
    if (line.type !== 'context' || line.text.length <= SHORTEN_FROM) {
      return { pieces: null, partner: null };
    }
    // Unchanged text only, so the pieces are equal / elided as they are.
    const pieces = shortenStretch(line.text, 'head') as InlinePiece[];
    return { pieces: pieces.length > 1 ? pieces : null, partner: null };
  });

  const budget: Budget = { diffs: 0 };
  let k = 0;
  while (k < lines.length) {
    if (lines[k].type !== 'del') {
      k++;
      continue;
    }
    const dels: number[] = [];
    while (k < lines.length && lines[k].type === 'del') dels.push(k++);
    const adds: number[] = [];
    while (k < lines.length && lines[k].type === 'add') adds.push(k++);
    for (const [d, a, segments] of alignRuns(lines, dels, adds, budget)) {
      const shorten = Math.max(lines[d].text.length, lines[a].text.length) > SHORTEN_FROM;
      const pieces = pairPieces(segments, shorten);
      details[d] = { pieces: sidePieces(pieces, 'del'), partner: a };
      details[a] = { pieces: sidePieces(pieces, 'add'), partner: d };
    }
  }
  return details;
}
