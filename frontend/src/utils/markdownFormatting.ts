/**
 * Pure text edits behind the doc editor's formatting toolbar (DocEditor).
 * Each helper takes the textarea's value and selection and returns the
 * replacement to apply -- the same `TextEdit` shape the editor's Tab /
 * image insertion edits use -- so the editor applies them through one
 * undo-friendly path and this module stays unit-testable without a DOM.
 *
 * Every action toggles: applying bold to bold text unwraps it, applying a
 * bullet to a bulleted line removes the bullet, and a heading button on a
 * line already at that level turns it back into a paragraph.
 */

/** Replace value[start, end) with `text`, then select [selStart, selEnd). */
export interface TextEdit {
  start: number;
  end: number;
  text: string;
  selStart: number;
  selEnd: number;
}

export type InlineFormat = 'bold' | 'italic' | 'strikethrough' | 'code';
export type LineFormat = 'h1' | 'h2' | 'h3' | 'bullet' | 'numbered' | 'quote';
export type MarkdownFormat = InlineFormat | LineFormat | 'link';

const INLINE_MARKERS: Record<InlineFormat, { marker: string; placeholder: string }> = {
  bold: { marker: '**', placeholder: 'bold text' },
  italic: { marker: '_', placeholder: 'italic text' },
  strikethrough: { marker: '~~', placeholder: 'strikethrough text' },
  code: { marker: '`', placeholder: 'code' },
};

const LINK_TEXT_PLACEHOLDER = 'link text';
const LINK_URL_PLACEHOLDER = 'url';

/** A heading prefix (`## `) or list / quote marker after any indentation. */
const HEADING_RE = /^(\s*)(#{1,6})[ \t]+/;
const BULLET_RE = /^(\s*)([-*+])[ \t]+/;
const NUMBERED_RE = /^(\s*)(\d+)[.)][ \t]+/;
const QUOTE_RE = /^(\s*)>[ \t]?/;

/**
 * The whole lines a selection touches, as [lineStart, blockEnd) (blockEnd
 * excludes the last line's newline). A selection ending right after a
 * newline does not touch the next line.
 */
export function selectedLines(value: string, start: number, end: number) {
  const lineStart = start === 0 ? 0 : value.lastIndexOf('\n', start - 1) + 1;
  const last = end > start && value[end - 1] === '\n' ? end - 1 : end;
  const newline = value.indexOf('\n', last);
  return { lineStart, blockEnd: newline === -1 ? value.length : newline };
}

// --- Inline (bold, italic, strikethrough, code) --------------------------------

/**
 * Wrap the selection in `marker` (e.g. `**`), or unwrap it when it -- or the
 * text just around it -- is already wrapped. Whitespace at the selection's
 * edges stays outside the markers (`** bold **` does not render). With no
 * selection a selected placeholder is inserted, so typing replaces it.
 */
export function inlineFormatEdit(
  value: string,
  start: number,
  end: number,
  format: InlineFormat,
): TextEdit {
  const { marker, placeholder } = INLINE_MARKERS[format];
  const m = marker.length;

  if (start === end) {
    // Caret inside an empty pair (`**|**`, left by a previous toggle): drop it.
    if (value.slice(start - m, start) === marker && value.slice(end, end + m) === marker) {
      return { start: start - m, end: end + m, text: '', selStart: start - m, selEnd: start - m };
    }
    const text = marker + placeholder + marker;
    return { start, end, text, selStart: start + m, selEnd: start + m + placeholder.length };
  }

  const selected = value.slice(start, end);
  const leading = selected.length - selected.trimStart().length;
  const trailing = selected.length - selected.trimEnd().length;
  const innerStart = start + leading;
  const innerEnd = Math.max(innerStart, end - trailing);
  const inner = value.slice(innerStart, innerEnd);

  // The selection itself is wrapped: `**bold**` -> `bold`.
  if (inner.length >= 2 * m && inner.startsWith(marker) && inner.endsWith(marker)) {
    const text = inner.slice(m, inner.length - m);
    return { start: innerStart, end: innerEnd, text, selStart: innerStart, selEnd: innerStart + text.length };
  }
  // The markers sit just outside the selection: `**[bold]**` -> `bold`.
  if (
    value.slice(innerStart - m, innerStart) === marker
    && value.slice(innerEnd, innerEnd + m) === marker
  ) {
    return {
      start: innerStart - m,
      end: innerEnd + m,
      text: inner,
      selStart: innerStart - m,
      selEnd: innerStart - m + inner.length,
    };
  }
  return {
    start: innerStart,
    end: innerEnd,
    text: marker + inner + marker,
    selStart: innerStart + m,
    selEnd: innerStart + m + inner.length,
  };
}

// --- Link -----------------------------------------------------------------------

function looksLikeUrl(text: string): boolean {
  return /^(https?:\/\/|mailto:)\S+$/i.test(text) && !/\s/.test(text);
}

/**
 * `[text](url)` around the selection. Selected prose becomes the text and
 * the url placeholder is selected; a selected URL becomes the target and the
 * text placeholder is selected; with nothing selected both are placeholders
 * and the text one is selected.
 */
export function linkEdit(value: string, start: number, end: number): TextEdit {
  const selected = value.slice(start, end).trim();
  const leading = value.slice(start, end).length - value.slice(start, end).trimStart().length;
  const at = start + leading;
  const to = at + selected.length;
  if (selected === '') {
    const text = `[${LINK_TEXT_PLACEHOLDER}](${LINK_URL_PLACEHOLDER})`;
    return { start, end, text, selStart: start + 1, selEnd: start + 1 + LINK_TEXT_PLACEHOLDER.length };
  }
  if (looksLikeUrl(selected)) {
    const text = `[${LINK_TEXT_PLACEHOLDER}](${selected})`;
    return { start: at, end: to, text, selStart: at + 1, selEnd: at + 1 + LINK_TEXT_PLACEHOLDER.length };
  }
  const text = `[${selected}](${LINK_URL_PLACEHOLDER})`;
  const urlStart = at + selected.length + 3;
  return { start: at, end: to, text, selStart: urlStart, selEnd: urlStart + LINK_URL_PLACEHOLDER.length };
}

// --- Lines (headings, lists, quote) -----------------------------------------------

/** A line split into its indentation, any block prefix, and the rest. */
interface LineParts {
  indent: string;
  prefix: string;
  rest: string;
}

function splitLine(line: string): LineParts {
  for (const re of [HEADING_RE, BULLET_RE, NUMBERED_RE, QUOTE_RE]) {
    const match = re.exec(line);
    if (match) {
      return {
        indent: match[1],
        prefix: match[0].slice(match[1].length),
        rest: line.slice(match[0].length),
      };
    }
  }
  const indent = /^\s*/.exec(line)?.[0] ?? '';
  return { indent, prefix: '', rest: line.slice(indent.length) };
}

function hasFormat(prefix: string, format: LineFormat): boolean {
  switch (format) {
    case 'h1': return /^#[ \t]+$/.test(prefix);
    case 'h2': return /^##[ \t]+$/.test(prefix);
    case 'h3': return /^###[ \t]+$/.test(prefix);
    case 'bullet': return /^[-*+][ \t]+$/.test(prefix);
    case 'numbered': return /^\d+[.)][ \t]+$/.test(prefix);
    case 'quote': return /^>[ \t]?$/.test(prefix);
  }
}

function prefixFor(format: LineFormat, index: number): string {
  switch (format) {
    case 'h1': return '# ';
    case 'h2': return '## ';
    case 'h3': return '### ';
    case 'bullet': return '- ';
    case 'numbered': return `${index + 1}. `;
    case 'quote': return '> ';
  }
}

/**
 * Toggle a line format on every line the selection touches. When every
 * non-empty touched line already has the format it is removed, otherwise it
 * is applied (replacing another heading / list / quote prefix). Empty lines
 * inside a multi-line selection are left alone; a bare cursor on an empty
 * line gets the prefix so typing continues the list / heading. With a
 * selection the whole changed block stays selected; a bare caret keeps its
 * place in the text.
 */
export function lineFormatEdit(
  value: string,
  start: number,
  end: number,
  format: LineFormat,
): TextEdit {
  const { lineStart, blockEnd } = selectedLines(value, start, end);
  const lines = value.slice(lineStart, blockEnd).split('\n');
  const parts = lines.map(splitLine);
  const single = lines.length === 1;
  const touched = parts.filter((_, i) => single || lines[i].trim() !== '');
  const remove = touched.length > 0 && touched.every((p) => hasFormat(p.prefix, format));

  let listIndex = 0;
  const next = parts.map((p, i) => {
    if (!single && lines[i].trim() === '') return lines[i];
    if (remove) return p.indent + p.rest;
    const prefix = prefixFor(format, listIndex);
    listIndex += 1;
    return p.indent + prefix + p.rest;
  });

  const text = next.join('\n');
  if (start !== end) {
    return { start: lineStart, end: blockEnd, text, selStart: lineStart, selEnd: lineStart + text.length };
  }
  // Bare caret: move it by its own line's length change, never before the
  // line's start (a caret inside a removed prefix lands at the start).
  const delta = next[0].length - lines[0].length;
  const caret = Math.max(lineStart, start + delta);
  return { start: lineStart, end: blockEnd, text, selStart: caret, selEnd: caret };
}

/** Dispatch one toolbar / shortcut action to its edit. */
export function markdownFormatEdit(
  value: string,
  start: number,
  end: number,
  format: MarkdownFormat,
): TextEdit {
  switch (format) {
    case 'bold':
    case 'italic':
    case 'strikethrough':
    case 'code':
      return inlineFormatEdit(value, start, end, format);
    case 'link':
      return linkEdit(value, start, end);
    default:
      return lineFormatEdit(value, start, end, format);
  }
}
