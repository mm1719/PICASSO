import { defineConfig } from '@playwright/test'

const externalBaseURL = process.env.PLAYWRIGHT_BASE_URL

export default defineConfig({
  testDir: './e2e',
  use: { baseURL: externalBaseURL ?? 'http://127.0.0.1:4173' },
  webServer: externalBaseURL ? undefined : {
    command: 'npm run dev -- --config vite.e2e.config.ts --host 127.0.0.1 --port 4173',
    url: 'http://127.0.0.1:4173',
    reuseExistingServer: false,
  },
})
