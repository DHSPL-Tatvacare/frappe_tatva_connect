import { test, expect } from "@playwright/test";
import {
  loadCreds,
  storageStateFor,
  forbiddenVerticalsFor,
  Persona,
} from "./auth.setup";

/**
 * leads.spec.ts — A1/A2 horizontal grain-leak check at the list surface.
 *
 * For each grain persona, load /crm/leads with that persona's session and
 * assert the visible rows contain NO out-of-grain vertical. The forbidden
 * verticals are data-driven from grain_key:
 *   grain_4 (TatvaPractice::India::InsideSales) → must show no GoodFlip rows
 *   grain_5 (GoodFlip::B2C::InsideSales)        → must show no TatvaPractice rows
 * (grain_4 & grain_5 share program "InsideSales" — the A2 trap.)
 *
 * Only personas that hold a concrete grain are exercised. System Manager /
 * no-grain personas are skipped here (they belong to the sweep tier).
 */

const creds: Persona[] = loadCreds();
const grainPersonas = creds.filter(
  (c) => (c.grain_key || "").includes("::") && forbiddenVerticalsFor(c.grain_key).length > 0,
);

for (const cred of grainPersonas) {
  const forbidden = forbiddenVerticalsFor(cred.grain_key);

  test.describe(`leads visibility — ${cred.persona} (${cred.grain_key})`, () => {
    test.use({ storageState: storageStateFor(cred.persona) });

    test(`no out-of-grain vertical rows visible`, async ({ page }) => {
      await page.goto("/crm/leads", { waitUntil: "networkidle" });

      // The SPA renders rows lazily; give the list a beat to populate or to
      // settle on an empty state. We assert against whatever is rendered.
      await page
        .waitForLoadState("networkidle", { timeout: 15000 })
        .catch(() => undefined);

      // Grab the full rendered text of the leads list region. We deliberately
      // read the whole document body text rather than a brittle row selector:
      // if a forbidden vertical string appears ANYWHERE in the rendered list,
      // that is a leak worth failing on.
      const bodyText = (await page.locator("body").innerText()).toString();

      for (const vertical of forbidden) {
        expect(
          bodyText,
          `persona ${cred.persona} (grain ${cred.grain_key}) must NOT see ` +
            `out-of-grain vertical "${vertical}" in /crm/leads`,
        ).not.toContain(vertical);
      }
    });

    test(`leads list API ⊆ grain (no forbidden vertical in custom_vertical)`, async ({
      request,
    }) => {
      // Cross-check the list at the data layer the SPA itself uses. We only
      // request the fieldname the grain layer is supposed to scope on. If the
      // server returns rows whose vertical is forbidden, that is an escalation
      // regardless of what the DOM rendered.
      const resp = await request.get(
        "/api/method/frappe.client.get_list" +
          "?doctype=CRM%20Lead" +
          '&fields=["name","custom_vertical"]' +
          "&limit_page_length=0",
      );
      // A non-2xx here is acceptable (the field may be permlevel-gated); we
      // only assert on rows that ARE returned.
      if (!resp.ok()) return;
      const payload = await resp.json().catch(() => ({}));
      const rows: Array<Record<string, unknown>> = payload?.message || [];
      for (const row of rows) {
        const v = String(row?.custom_vertical ?? "");
        for (const vertical of forbidden) {
          expect(
            v,
            `persona ${cred.persona} got a CRM Lead in forbidden vertical ` +
              `"${vertical}" (row ${String(row?.name)}) — grain LEAK`,
          ).not.toBe(vertical);
        }
      }
    });
  });
}

test("grain personas were discovered from creds", () => {
  expect(
    grainPersonas.length,
    "expected at least one grain persona (grain_4 / grain_5) in creds",
  ).toBeGreaterThan(0);
});
