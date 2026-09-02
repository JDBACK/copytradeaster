import { defineConfig } from 'vitest/config'

// Node-environment unit tests for pure modules (api, wizard, home, wallet,
// format) plus read-only renderToStaticMarkup UI contract tests for the
// positions card.  Component rendering is accepted manually on the testnet
// for part 1; the server-side flow is covered by pytest FakeAster integration.
export default defineConfig({
  test: {
    environment: 'node',
    include: ['src/**/*.test.ts', 'src/**/*.test.tsx'],
  },
})
