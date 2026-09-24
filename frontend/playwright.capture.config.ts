import { defineConfig, devices } from '@playwright/test'

// Records the scripted demo walkthrough (e2e/demo.capture.ts) as a silent video.
// Run through scripts/run_demo.py --capture, which starts a demo stack with real models.
export default defineConfig({
  testDir: './e2e',
  testMatch: '*.capture.ts',
  timeout: 15 * 60_000,
  expect: { timeout: 5 * 60_000 },
  outputDir: './e2e-results',
  reporter: [['list']],
  use: {
    baseURL: process.env.E2E_BASE_URL ?? 'http://localhost:8020',
    // Headed: headless Chromium does not render PDFs inside the document viewer.
    headless: false,
    video: { mode: 'on', size: { width: 1440, height: 900 } },
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'], viewport: { width: 1440, height: 900 } } }],
})
