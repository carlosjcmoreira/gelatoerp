import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './tests/browser',
  testMatch: '**/compras_catalogue_scroll.spec.mjs',
  fullyParallel: false,
  workers: 1,
  timeout: 30_000,
  reporter: 'list',
  use: {
    baseURL: 'http://127.0.0.1:8766',
    browserName: 'chromium',
    headless: true,
    launchOptions: {
      executablePath: '/repl/tools/bin/chromium',
      args: ['--no-sandbox'],
    },
  },
  webServer: {
    command: 'PYTHONPATH=. python tests/browser/compras_catalogue_app.py',
    url: 'http://127.0.0.1:8766/_health',
    timeout: 30_000,
    reuseExistingServer: false,
  },
});