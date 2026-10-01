import js from '@eslint/js'
import globals from 'globals'
import reactHooks from 'eslint-plugin-react-hooks'
import tseslint from 'typescript-eslint'
import { defineConfig, globalIgnores } from 'eslint/config'

export default defineConfig([
  globalIgnores(['dist']),
  {
    files: ['**/*.{ts,tsx}'],
    extends: [
      js.configs.recommended,
      tseslint.configs.recommended,
      reactHooks.configs.flat.recommended,
    ],
    languageOptions: {
      ecmaVersion: 2020,
      globals: globals.browser,
    },
    rules: {
      // eslint-plugin-react-hooks 6+ ships rules derived from the React
      // Compiler alongside the classic `rules-of-hooks` / `exhaustive-deps`.
      // They assume code written for the compiler (no React Compiler is in
      // this build) and flag idioms the codebase uses on purpose: "latest
      // value" refs assigned during render, modals that reset local state in
      // an effect when they open, hand-rolled useMemo/useCallback that the
      // compiler would otherwise own, and test probes that count renders.
      // Fixing them is a behaviour-affecting refactor, not a lint cleanup,
      // so they stay off until that work is done. `rules-of-hooks` (error)
      // and `exhaustive-deps` (warning) are unchanged.
      'react-hooks/set-state-in-effect': 'off',
      'react-hooks/refs': 'off',
      'react-hooks/immutability': 'off',
      'react-hooks/preserve-manual-memoization': 'off',
    },
  },
])
