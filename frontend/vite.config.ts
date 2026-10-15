import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { viteStaticCopy } from 'vite-plugin-static-copy'
import { themeOverridePlugin } from './themeOverridePlugin'

export default defineConfig({
  plugins: [
    react(),
    // pdf.js runtime assets must be self-hosted: the deployment has no network
    // egress, and the FastAPI server only serves multi-segment paths under the
    // /assets mount (quest.py), so everything lands in dist/assets/pdfjs/.
    viteStaticCopy({
      // stripBase drops the leading node_modules/pdfjs-dist segments so the
      // directories land at dist/assets/pdfjs/{cmaps,standard_fonts,wasm,iccs}.
      targets: [
        { src: 'node_modules/pdfjs-dist/cmaps', dest: 'assets/pdfjs', rename: { stripBase: 2 } },
        { src: 'node_modules/pdfjs-dist/standard_fonts', dest: 'assets/pdfjs', rename: { stripBase: 2 } },
        { src: 'node_modules/pdfjs-dist/wasm', dest: 'assets/pdfjs', rename: { stripBase: 2 } },
        { src: 'node_modules/pdfjs-dist/iccs', dest: 'assets/pdfjs', rename: { stripBase: 2 } },
      ],
    }),
  ],

  // Rewrites every `@media (prefers-color-scheme: ...)` block so the
  // Settings > Appearance light/dark/auto selector (<html data-theme>) can
  // override the OS scheme. See themeOverridePlugin.ts.
  css: {
    postcss: {
      plugins: [themeOverridePlugin()],
    },
  },

  // Minified JS and CSS (Vite's default esbuild minifier). Source maps stay
  // on, next to each chunk, so browser devtools and stack traces map back to
  // the original sources. Heavy views are split into lazy chunks with
  // React.lazy (see LazyBoundary.tsx); /assets serves them like any asset.
  build: {
    outDir: 'dist',
    assetsDir: 'assets',
    sourcemap: true,
  },

  base: '/',
})
