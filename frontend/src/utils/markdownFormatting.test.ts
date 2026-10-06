import { describe, expect, it } from 'vitest';
import {
  inlineFormatEdit,
  lineFormatEdit,
  linkEdit,
  markdownFormatEdit,
  type MarkdownFormat,
  type TextEdit,
} from './markdownFormatting';

/** Apply an edit the way the editor does and report the result + selection. */
function apply(value: string, edit: TextEdit) {
  const text = value.slice(0, edit.start) + edit.text + value.slice(edit.end);
  return { text, selected: text.slice(edit.selStart, edit.selEnd), caret: edit.selStart };
}

/** Run a format over `[` ... `]`-marked selection in `marked`. */
function format(marked: string, kind: MarkdownFormat) {
  const start = marked.indexOf('[');
  const end = marked.indexOf(']') - 1;
  const value = marked.replace('[', '').replace(']', '');
  return apply(value, markdownFormatEdit(value, start, end, kind));
}

describe('inlineFormatEdit', () => {
  it('wraps a selection and keeps the inner text selected', () => {
    expect(format('say [hello] there', 'bold')).toMatchObject({
      text: 'say **hello** there',
      selected: 'hello',
    });
    expect(format('say [hello] there', 'italic').text).toBe('say _hello_ there');
    expect(format('say [hello] there', 'strikethrough').text).toBe('say ~~hello~~ there');
    expect(format('say [hello] there', 'code').text).toBe('say `hello` there');
  });

  it('keeps edge whitespace outside the markers', () => {
    expect(format('say[ hello ]there', 'bold')).toMatchObject({
      text: 'say **hello** there',
      selected: 'hello',
    });
  });

  it('unwraps a selection that is already wrapped', () => {
    expect(format('say [**hello**] there', 'bold')).toMatchObject({
      text: 'say hello there',
      selected: 'hello',
    });
  });

  it('unwraps when the markers sit just outside the selection', () => {
    expect(format('say **[hello]** there', 'bold')).toMatchObject({
      text: 'say hello there',
      selected: 'hello',
    });
  });

  it('inserts a selected placeholder at a bare caret', () => {
    expect(format('say [] there', 'bold')).toMatchObject({
      text: 'say **bold text** there',
      selected: 'bold text',
    });
  });

  it('removes an empty pair the caret sits in', () => {
    const value = 'say **** there';
    const edit = inlineFormatEdit(value, 6, 6, 'bold');
    expect(apply(value, edit)).toMatchObject({ text: 'say  there', caret: 4 });
  });
});

describe('linkEdit', () => {
  it('turns selected prose into the link text and selects the url placeholder', () => {
    expect(format('see [the docs] now', 'link')).toMatchObject({
      text: 'see [the docs](url) now',
      selected: 'url',
    });
  });

  it('turns a selected URL into the target and selects the text placeholder', () => {
    expect(format('see [https://x.io/a?b=1] now', 'link')).toMatchObject({
      text: 'see [link text](https://x.io/a?b=1) now',
      selected: 'link text',
    });
  });

  it('inserts both placeholders at a bare caret', () => {
    const value = 'see ';
    expect(apply(value, linkEdit(value, 4, 4))).toMatchObject({
      text: 'see [link text](url)',
      selected: 'link text',
    });
  });
});

describe('lineFormatEdit', () => {
  it('adds a heading to the caret line and keeps the caret in place', () => {
    const value = 'intro\nTitle here\nbody';
    const edit = lineFormatEdit(value, 11, 11, 'h2');
    expect(apply(value, edit)).toMatchObject({ text: 'intro\n## Title here\nbody', caret: 14 });
  });

  it('swaps one heading level for another and toggles the same level off', () => {
    expect(format('[## Title]', 'h1').text).toBe('# Title');
    expect(format('[## Title]', 'h2').text).toBe('Title');
    expect(format('[# Title]', 'h3').text).toBe('### Title');
  });

  it('a caret inside a removed prefix lands at the line start', () => {
    const value = '## Title';
    const edit = lineFormatEdit(value, 1, 1, 'h2');
    expect(apply(value, edit)).toMatchObject({ text: 'Title', caret: 0 });
  });

  it('bullets every selected line, skipping blank ones, and keeps the block selected', () => {
    expect(format('[one\n\ntwo\nthree]', 'bullet')).toMatchObject({
      text: '- one\n\n- two\n- three',
      selected: '- one\n\n- two\n- three',
    });
  });

  it('numbers lines sequentially and converts an existing bullet list', () => {
    expect(format('[- a\n- b\n* c]', 'numbered').text).toBe('1. a\n2. b\n3. c');
  });

  it('removes the list when every selected line already has it', () => {
    expect(format('[1. a\n2. b]', 'numbered').text).toBe('a\nb');
    expect(format('[- a\n- b]', 'bullet').text).toBe('a\nb');
  });

  it('applies the format when only some lines have it', () => {
    expect(format('[- a\nb]', 'bullet').text).toBe('- a\n- b');
  });

  it('keeps indentation in front of the marker', () => {
    expect(format('  [nested]', 'bullet').text).toBe('  - nested');
    expect(format('[  - nested]', 'bullet').text).toBe('  nested');
  });

  it('quotes and unquotes', () => {
    expect(format('[a\nb]', 'quote').text).toBe('> a\n> b');
    expect(format('[> a\n>b]', 'quote').text).toBe('a\nb');
  });

  it('a bare caret on an empty line gets the prefix', () => {
    const value = 'x\n';
    expect(apply(value, lineFormatEdit(value, 2, 2, 'bullet'))).toMatchObject({
      text: 'x\n- ',
      caret: 4,
    });
  });

  it('a selection ending right after a newline does not touch the next line', () => {
    expect(format('[a\n]b', 'bullet').text).toBe('- a\nb');
  });
});
