import { test as setup, expect, request as pwRequest } from "@playwright/test";
import * as fs from "fs";

/**
 * auth.setup.ts — the gatekeeper + login bootstrap.
 *
 * 1. COMMS-OFF INTERLOCK (mandatory). We do NOT re-check the five
 *    CRM Tatva Automation switches from here (no whitelisted endpoint to do
 *    so safely). Instead the Python tcsec lane runs assert_comms_off() and
 *    ONLY THEN exports AUTHZ_COMMS_OFF=1 before invoking Playwright. If that
 *    env var is not exactly "1", setup throws and no browser ever launches.
 *
 * 2. For each persona in the creds file, log in through the REAL CRM login
 *    form (#login_email / #login_password). If the form selectors fail, fall
 *    back to POST /api/method/login (sets the session cookie the same way).
 *    The authenticated storageState is saved to .auth/<persona>.json.
 */

// The creds/persona helpers live in `harness.ts` — a NON-test module, because Playwright refuses to
// let a spec import a test file and this IS one. Re-exported so every existing import keeps working.
import { loadCreds, storageStateFor, forbiddenVerticalsFor, AUTH_STATE_DIR as AUTH_DIR } from "./harness";
import type { Persona } from "./harness";

export { loadCreds, storageStateFor, forbiddenVerticalsFor };
export type { Persona };

setup("comms-off interlock", async () => {
  expect(
    process.env.AUTHZ_COMMS_OFF,
    "AUTHZ_COMMS_OFF must be exactly '1' — the Python tcsec lane sets it ONLY " +
      "after assert_comms_off() confirms WATI / Acefone / follow-up / both FCM " +
      "push channels are all OFF. Refusing to launch browsers.",
  ).toBe("1");
});

setup("authenticate all personas", async ({ baseURL }) => {
  // Hard dependency on the interlock having passed.
  expect(process.env.AUTHZ_COMMS_OFF).toBe("1");

  fs.mkdirSync(AUTH_DIR, { recursive: true });
  const creds = loadCreds();
  const base = baseURL || "http://dev.localhost:8000";

  const { chromium } = await import("@playwright/test");

  for (const cred of creds) {
    let loggedIn = false;

    // --- Path 1: the real CRM login FORM ---
    const browser = await chromium.launch();
    const context = await browser.newContext({ baseURL: base });
    const formPage = await context.newPage();
    try {
      await formPage.goto("/login", { waitUntil: "domcontentloaded" });
      await formPage.fill("#login_email", cred.email, { timeout: 8000 });
      await formPage.fill("#login_password", cred.password, { timeout: 8000 });
      // Primary login button (Frappe stock login form).
      await formPage.click("button.btn-login, .btn-login, button[type=submit]", {
        timeout: 8000,
      });
      // A successful login navigates away from /login.
      await formPage.waitForURL((url) => !url.pathname.includes("/login"), {
        timeout: 12000,
      });
      await context.storageState({ path: storageStateFor(cred.persona) });
      loggedIn = true;
    } catch (formErr) {
      // --- Path 2: API login fallback (sets the same session cookie) ---
      const apiCtx = await pwRequest.newContext({ baseURL: base });
      try {
        const resp = await apiCtx.post("/api/method/login", {
          data: { usr: cred.email, pwd: cred.password },
        });
        if (!resp.ok()) {
          throw new Error(
            `Login failed for persona "${cred.persona}" (${cred.email}): ` +
              `form path errored (${(formErr as Error).message}) AND ` +
              `/api/method/login returned ${resp.status()}.`,
          );
        }
        // Persist the cookie jar from the API request context.
        await apiCtx.storageState({ path: storageStateFor(cred.persona) });
        loggedIn = true;
      } finally {
        await apiCtx.dispose();
      }
    } finally {
      await browser.close();
    }

    expect(loggedIn, `no session for persona ${cred.persona}`).toBe(true);
  }
});
