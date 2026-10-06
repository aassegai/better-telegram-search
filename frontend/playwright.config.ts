import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './tests',
  workers: 1,
  use: { baseURL: 'http://127.0.0.1:8766', browserName: 'chromium', trace: 'off' },
  webServer: {
    command: 'uv run --no-sync python ../scripts/e2e_server.py',
    url: 'http://127.0.0.1:8766',
    reuseExistingServer: false,
    timeout: 30000,
  },
});
