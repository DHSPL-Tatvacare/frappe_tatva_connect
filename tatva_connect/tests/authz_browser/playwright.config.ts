import { defineConfig, devices } from "@playwright/test";

/**
 * Playwright config for the authz/VAPT suite.
 *
 * baseURL targets the local devbench CRM SPA. Every spec depends on the
 * `setup` project, which logs each persona in and writes a storageState
 * file under `.auth/<persona>.json`. Specs then run with the relevant
 * persona's storageState so the browser carries that user's real session.
 *
 * SAFETY: the setup project asserts AUTHZ_COMMS_OFF === "1" before any
 * browser launches (see auth.setup.ts). The Python tcsec lane sets that
 * env var ONLY after assert_comms_off() has passed.
 */
export default defineConfig({
  testDir: ".",
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: 0,
  workers: 1,
  reporter: [["list"], ["json", { outputFile: ".auth/playwright-report.json" }]],
  timeout: 60_000,
  expect: { timeout: 15_000 },

  use: {
    baseURL: process.env.AUTHZ_BASE_URL || "http://dev.localhost:8000",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    ignoreHTTPSErrors: true,
  },

  projects: [
    {
      name: "setup",
      testMatch: /auth\.setup\.ts/,
    },
    {
      name: "authz",
      testMatch: /.*\.spec\.ts/,
      dependencies: ["setup"],
      use: { ...devices["Desktop Chrome"] },
    },
  ],
});
