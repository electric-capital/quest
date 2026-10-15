// The code-highlighting and math chunk: highlight.js and KaTeX, the bulk of
// the rendering weight, kept out of the main bundle. It holds the rehype
// plugins RichMarkdown adds (rehype-highlight with highlight.js's common
// languages, rehype-katex), the KaTeX stylesheet (its @font-face rules make
// Vite bundle the self-hosted KaTeX fonts) and the Python highlighter for
// run_python tool inputs. Only hooks/useCodeAndMath.ts imports this, and
// only dynamically; a static import anywhere else would pull it back into
// the main chunk.
import hljs from 'highlight.js/lib/core';
import python from 'highlight.js/lib/languages/python';
import rehypeHighlight from 'rehype-highlight';
import rehypeKatex from 'rehype-katex';
import 'katex/dist/katex.min.css';

hljs.registerLanguage('python', python);

/** Python source as highlight.js HTML (escaped, `hljs-*` spans). */
export function highlightPython(code: string): string {
  return hljs.highlight(code, { language: 'python' }).value;
}

export { rehypeHighlight, rehypeKatex };
