import { defineConfig, devices } from '@playwright/test'

// Browser tests against a running stack (API + worker + built UI); see e2e/README.md.
export default defineConfig({
  testDir: './e2e',
  testMatch: '*.e2e.ts',
  timeout: 120_000,
  expect: { timeout: 30_000 },
  retries: 0,
  outputDir: './e2e-results',
  reporter: [['list']],
  use: {
    baseURL: process.env.E2E_BASE_URL ?? 'http://localhost:8000',
    trace: 'retain-on-failure',
    video: process.env.E2E_VIDEO ? { mode: 'on', size: { width: 1440, height: 900 } } : 'off',
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'], viewport: { width: 1440, height: 900 } } }],
})
