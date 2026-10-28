import { describe, expect, it } from 'vitest';
import type { SkillContentDiffLine } from '../api/types';
import { inlineLineDetails, type LineDetail } from './inlineDiff';

const context = (text: string): SkillContentDiffLine => (
  { type: 'context', old_line: 1, new_line: 1, text }
);
const del = (text: string): SkillContentDiffLine => (
  { type: 'del', old_line: 1, new_line: null, text }
);
const add = (text: string): SkillContentDiffLine => (
  { type: 'add', old_line: null, new_line: 1, text }
);

/** `[change]`, `{…N}` for N elided characters, plain text as is. */
const show = (detail: LineDetail) => detail.pieces?.map((piece) => (
  piece.type === 'change' ? `[${piece.text}]`
    : piece.type === 'elided' ? `{…${piece.text.length}}`
      : piece.text
)).join('') ?? null;

const LEAD = 'We spent most of September on the security review, which turned up two '
  + 'issues in the sharing flow that needed a redesign before anything could ship.';
const TAIL = 'Marketing will start the campaign two weeks before the launch, with a blog '
  + 'post, a webinar and outreach to the beta customers who asked to be told first.';
const paragraph = (quarter: string) =>
  `${LEAD} The launch is planned for ${quarter}, pending the security review. ${TAIL}`;

describe('inlineLineDetails', () => {
  it('highlights the changed word of a long paragraph and keeps its sentence', () => {
    const [before, after] = inlineLineDetails([del(paragraph('Q3')), add(paragraph('Q4'))]);
    expect(before.partner).toBe(1);
    expect(after.partner).toBe(0);
    expect(show(before)).toBe(
      `{…${LEAD.length + 1}}The launch is planned for [Q3,] pending the security review.{…${TAIL.length + 1}}`,
    );
    expect(show(after)).toBe(
      `{…${LEAD.length + 1}}The launch is planned for [Q4,] pending the security review.{…${TAIL.length + 1}}`,
    );
  });

  it('keeps every line exactly: the pieces join back to the text', () => {
    const lines = [
      context(paragraph('Q2')),
      del(paragraph('Q3')),
      del('A short line.'),
      add(paragraph('Q4')),
      add('A short line, edited.'),
      del('Totally different words here.'),
      add('Nothing in common at all.'),
    ];
    inlineLineDetails(lines).forEach((detail, i) => {
      if (detail.pieces) expect(detail.pieces.map((p) => p.text).join('')).toBe(lines[i].text);
    });
  });

  it('leaves short lines whole', () => {
    const [before, after] = inlineLineDetails([
      del('Drinks drip coffee every morning.'),
      add('Drinks pour-over coffee every morning.'),
    ]);
    expect(show(before)).toBe('Drinks [drip] coffee every morning.');
    expect(show(after)).toBe('Drinks [pour-over] coffee every morning.');
  });

  it('pairs a changed line past a deleted one rather than by position', () => {
    const details = inlineLineDetails([
      del('An unrelated paragraph that was removed outright.'),
      del('The launch is planned for Q3, pending review.'),
      add('The launch is planned for Q4, pending review.'),
    ]);
    expect(details[0]).toEqual({ pieces: null, partner: null });
    expect(details[1].partner).toBe(2);
    expect(show(details[2])).toBe('The launch is planned for [Q4,] pending review.');
  });

  it('leaves a rewrite (too few shared words) as plain rows', () => {
    const details = inlineLineDetails([
      del('Ship the beta to ten customers first.'),
      add('Everyone gets the release on launch day.'),
    ]);
    expect(details).toEqual([{ pieces: null, partner: null }, { pieces: null, partner: null }]);
  });

  it('joins a rewritten phrase across a short shared word', () => {
    const [before, after] = inlineLineDetails([
      del('We will ship the beta to the first ten customers in the spring.'),
      add('We plan to give a preview to a handful of customers in the spring.'),
    ]);
    expect(show(before)).toBe('We [will ship the beta to the first ten] customers in the spring.');
    expect(show(after)).toBe('We [plan to give a preview to a handful of] customers in the spring.');
  });

  it('shortens a long context line to its opening and leaves short ones alone', () => {
    const [long, short] = inlineLineDetails([context(paragraph('Q3')), context('## Timeline')]);
    expect(show(long)).toBe(`${LEAD}{…${paragraph('Q3').length - LEAD.length}}`);
    expect(long.partner).toBeNull();
    expect(short.pieces).toBeNull();
  });

  it('elides the long unchanged middle between two far-apart changes', () => {
    const before = `Alpha ${paragraph('Q3')} omega.`;
    const after = `Beta ${paragraph('Q3')} zeta.`;
    const [old] = inlineLineDetails([del(before), add(after)]);
    const pieces = old.pieces!;
    expect(pieces[0]).toEqual({ type: 'change', text: 'Alpha' });
    expect(pieces[pieces.length - 1]).toEqual({ type: 'change', text: 'omega.' });
    expect(pieces.filter((p) => p.type === 'elided')).toHaveLength(1);
  });
});
