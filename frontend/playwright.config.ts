import { defineConfig, devices } from '@playwright/test';

// Browser end-to-end tests. Needs the Firebase emulators, the test-only API launcher
// (backend: python -m tests.e2e_server) and `VITE_USE_EMULATORS=true npx vite`.
export default defineConfig({
  testDir: 'e2e',
  timeout: 120_000,
  expect: { timeout: 20_000 },
  workers: 1,
  // E2E_BASE_URL=http://<lan-ip>:5173 checks the app the way another device on the network sees it.
  use: { baseURL: process.env.E2E_BASE_URL || 'http://localhost:5173', trace: 'retain-on-failure', screenshot: 'only-on-failure' },
  projects: [
    { name: 'mobile', use: { ...devices['Pixel 7'] } },
    { name: 'desktop', use: { ...devices['Desktop Chrome'] } },
  ],
});
