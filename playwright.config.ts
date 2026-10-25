import { defineConfig } from '@playwright/test';
import path from 'node:path';

export default defineConfig({
  testDir: './tests', testMatch: '**/*.spec.ts', fullyParallel: false, workers: 1,
  timeout: 120_000, expect: { timeout: 12_000 },
  reporter: [['list'], ['html', {open:'never'}]],
  use: { baseURL:'http://127.0.0.1:8765', viewport:{width:1380,height:880},
    channel: process.platform === 'win32' ? 'msedge' : 'chromium', screenshot:'only-on-failure', trace:'retain-on-failure' },
  webServer: { command: `"${path.resolve('.venv',process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python')}" run.py --web --port 8765`,
    url:'http://127.0.0.1:8765', reuseExistingServer:!process.env.CI, timeout:30_000 },
});
