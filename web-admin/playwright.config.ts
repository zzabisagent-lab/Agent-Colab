import { defineConfig } from '@playwright/test'

export default defineConfig({
  testDir: './tests',
  timeout: 30_000,
  use: {
    baseURL: process.env.WEB_ADMIN_URL ?? 'http://127.0.0.1:5173/admin',
    headless: true,
    // hosts without root use a wrapper that adds the extracted Chromium system libraries
    launchOptions: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE
      ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE }
      : {},
  },
  reporter: [['list']],
})
