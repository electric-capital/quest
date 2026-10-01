// Frontend unit tests (`npm test`): hooks, contexts and pure helpers under
// jsdom. Kept separate from vite.config.ts so the build-only plugins (static
// asset copy, theme override PostCSS) stay out of the test pipeline.
import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  test: {
    environment: 'jsdom',
    include: ['src/**/*.test.{ts,tsx}'],
  },
})
