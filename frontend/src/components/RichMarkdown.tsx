/**
 * The app's markdown renderer: ReactMarkdown with the shared plugin stack --
 * GFM, soft line breaks, `$` math with the currency guard, syntax
 * highlighting of fenced code (rehype-highlight) and KaTeX math
 * (rehype-katex). Assistant messages, the streaming bubble, the compaction
 * summary, the FileViewerModal `.md` preview and the Quest Docs viewer,
 * editor preview and History all render through it.
 *
 * The two rehype plugins come from the lazy code-and-math chunk
 * (hooks/useCodeAndMath.ts), requested only by markdown that contains a code
 * fence or a `$`: markdown with neither renders identically without them, so
 * it never waits for the chunk. Markdown that has them renders plain until
 * the chunk lands, then re-renders; ChatPanel re-pins its scroll then.
 */
import { useMemo } from 'react';
import ReactMarkdown, { type Components } from 'react-markdown';
import remarkGfm from 'remark-gfm';
import remarkBreaks from 'remark-breaks';
import remarkMath from 'remark-math';
import { useCodeAndMath } from '../hooks/useCodeAndMath';
import remarkMathCurrencyGuard from '../utils/remarkMathCurrencyGuard';

const REMARK_PLUGINS = [remarkGfm, remarkBreaks, remarkMath, remarkMathCurrencyGuard];

/**
 * Whether the rehype plugins would change this markdown: rehype-highlight
 * only colours fenced blocks with a language (and only when `highlight`),
 * and remark-math only parses `$` delimiters.
 */
export function needsCodeAndMath(markdown: string, highlight = true): boolean {
  return markdown.includes('$') || (highlight && /```|~~~/.test(markdown));
}

interface RichMarkdownProps {
  children: string;
  components?: Components;
  /** Syntax-highlight fenced code blocks (default true; math always renders). */
  highlight?: boolean;
}

export function RichMarkdown({ children, components, highlight = true }: RichMarkdownProps) {
  const chunk = useCodeAndMath(needsCodeAndMath(children, highlight));
  const rehypePlugins = useMemo(() => {
    if (!chunk) return [];
    return highlight ? [chunk.rehypeHighlight, chunk.rehypeKatex] : [chunk.rehypeKatex];
  }, [chunk, highlight]);

  return (
    <ReactMarkdown remarkPlugins={REMARK_PLUGINS} rehypePlugins={rehypePlugins} components={components}>
      {children}
    </ReactMarkdown>
  );
}
