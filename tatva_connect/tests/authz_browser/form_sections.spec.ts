import { test, expect, Page } from "@playwright/test";
import { loadCreds, storageStateFor, Persona } from "./harness";

/**
 * form_sections.spec.ts — Phase 8 of
 * docs/plans/task-form-layer/2026-07-25-task-slots-to-sections-and-form-layer.md, in the real SPA.
 *
 * Before Phase 8 `type_config` answered with one flat list and `TaskModal.vue` rendered it as one
 * two-up grid: an activity type could declare sections and nothing on screen changed. Phase 8 returns
 * the same fields GROUPED by the `CRM Task Section` each one names, carrying that section's own title,
 * tab, display order and condition, and the modal renders a heading per group.
 *
 * Per plan §12 a rendered element is NOT a pass. This asserts, in order:
 *   1. SERVER TRUTH FIRST — the expectations below are read off `type_config`, so a fixture that is not
 *      what this spec thinks it is fails loudly instead of proving nothing;
 *   2. VALUES — both headings render with the titles the sections DECLARE, not placeholders;
 *   3. EXACT COUNTS — the number of fields under each heading equals the declaration's, and the
 *      fieldnames under each heading are exactly that section's;
 *   4. ABSENCE where absence is the point — section B's field is NOT in section A's DOM subtree, no
 *      field escaped into an unsectioned group, and with one tab declared there is no tab strip.
 *
 * FIXTURE (must exist on the target site, COMMITTED — a browser cannot see an open transaction). Two
 * seeded column sections are reused; no section is created. The lead and the type sit on the RUNNING
 * PERSONA'S OWN GRAIN — `grain_key` in `authz_creds.json`, "vertical::group::program" — because a grain
 * persona cannot read a lead outside it and the type picker is grain-scoped. In
 * `bench --site <site> console`, with V/G/P taken from that persona's grain_key:
 *
 *   import frappe
 *   V, G, P = "<vertical>", "<group>", "<program>"     # from authz_creds.json grain_key
 *   a, b = frappe.get_all("CRM Task Section", filters={"is_key_value": 0, "is_multi_row": 0},
 *                         fields=["name"], order_by="display_order")[:2]
 *   frappe.get_doc({"doctype": "CRM Task Type", "type_name": "ZZ Phase 8 Browser Probe",
 *       "vertical": V, "group": G, "program": P, "schema": [
 *           {"label": "ZZ P8 Alpha One", "fieldname": "zz_p8_a1", "fieldtype": "Data", "section": a.name},
 *           {"label": "ZZ P8 Alpha Two", "fieldname": "zz_p8_a2", "fieldtype": "Data", "section": a.name},
 *           {"label": "ZZ P8 Beta One",  "fieldname": "zz_p8_b1", "fieldtype": "Data", "section": b.name},
 *       ]}).insert(ignore_permissions=True)
 *   frappe.get_doc({"doctype": "CRM Lead", "first_name": "ZZ Phase8 Browser Lead",
 *       "mobile_no": "+919812600001", "custom_vertical": V, "custom_group": G,
 *       "custom_current_program": P}).insert(ignore_permissions=True)
 *   frappe.db.commit()
 *
 * `a` is the lower-display_order section, so it is the FIRST group and holds the two alpha fields.
 *
 * Harness: the config and `auth.setup.ts` in this folder. No new runner, no new login helper.
 */

const TYPE_LABEL = "ZZ Phase 8 Browser Probe";
const LEAD_FIRST_NAME = "ZZ Phase8 Browser Lead";
// The declaration this spec was written against. Read again off type_config below — these are here so a
// server that returned an empty or reshuffled layout cannot make both sides agree on nothing.
const ALPHA_FIELDS = ["zz_p8_a1", "zz_p8_a2"];
const BETA_FIELDS = ["zz_p8_b1"];

const creds: Persona[] = loadCreds();
const persona: Persona =
  creds.find((c) => c.persona === process.env.AUTHZ_PERSONA) || creds[0];

type Group = {
  section: string;
  title: string;
  tab: string;
  depends_on: string;
  fields: { fieldname: string; label: string }[];
};

/** One whitelisted read through the page's own session (same cookies the SPA uses). */
async function api(page: Page, method: string, params: Record<string, string> = {}) {
  const qs = new URLSearchParams(params).toString();
  const res = await page.request.get(`/api/method/${method}${qs ? `?${qs}` : ""}`);
  expect(res.ok(), `${method} returned ${res.status()}`).toBeTruthy();
  return (await res.json()).message;
}

test.describe(`the activity form renders its declared sections — ${persona.persona}`, () => {
  // Desktop width on purpose: the modal lays fields out two-up only from the `sm` breakpoint, and the
  // sidebar overlays the lead header below it.
  test.use({
    storageState: storageStateFor(persona.persona),
    viewport: { width: 1680, height: 1000 },
  });

  test("two declared sections render as two headings, and neither holds the other's fields", async ({ page }) => {
    // ---- 1. server truth: what the type actually declares ------------------------------------
    const leads = await api(page, "frappe.client.get_list", {
      doctype: "CRM Lead",
      filters: JSON.stringify([["first_name", "=", LEAD_FIRST_NAME]]),
      fields: JSON.stringify(["name"]),
      limit_page_length: "1",
    });
    expect(leads?.length, `the fixture lead "${LEAD_FIRST_NAME}" is not readable on this site`).toBe(1);
    const lead: string = leads[0].name;

    const types = await api(page, "tatva_connect.activity.api.list_types_for_lead", { lead });
    const probe = (types || []).find((t: { label: string }) => t.label === TYPE_LABEL);
    expect(probe, `the fixture type "${TYPE_LABEL}" is not offered for this lead`).toBeTruthy();

    const cfg = await api(page, "tatva_connect.activity.api.type_config", { task_type: probe.name });
    const groups: Group[] = cfg.groups || [];
    expect(groups.length, "type_config did not return two groups — the layout is not declared").toBe(2);
    expect(
      groups.every((g) => g.section && g.title),
      "a group came back unsectioned or untitled — every probe field names a titled section",
    ).toBeTruthy();
    expect(
      groups.map((g) => g.fields.map((f) => f.fieldname)),
      "the declaration is not the two sections this spec was written against",
    ).toEqual([ALPHA_FIELDS, BETA_FIELDS]);
    const [alpha, beta] = groups;
    // One tab declared, so there must be no tab strip. Stated from the declaration, not assumed.
    expect(new Set(groups.map((g) => g.tab)).size, "the fixture declares more than one tab").toBe(1);

    // ---- 2. drive the real form --------------------------------------------------------------
    // The PWA service worker caches aggressively (plan §12) — cache-bust before trusting a screen.
    await page.goto(`/crm/leads/${lead}?cb=${Date.now()}#tasks`, { waitUntil: "networkidle" });
    // Stock CRM's onboarding Help panel auto-opens as a fixed overlay and swallows clicks. Close it.
    const helpPanel = page.locator("div.fixed.z-50.right-0");
    if (await helpPanel.count()) {
      await helpPanel.locator("button:has(svg.feather-x)").first().click();
      await expect(helpPanel).toHaveCount(0);
    }
    // "Log Activity" is reached through the bridge TatvaTasks publishes for exactly this (it is what the
    // lead header item calls), so the spec drives the product's own entry point without hunting a menu.
    await page.waitForFunction(() => typeof (window as any).__tcLogActivity === "function");
    await page.evaluate(() => (window as any).__tcLogActivity());
    await page.getByRole("button", { name: TYPE_LABEL, exact: true }).click();

    // ---- 3. VALUES: both headings render, with the titles the sections declare ----------------
    await expect(page.getByText(alpha.title, { exact: true })).toBeVisible();
    await expect(page.getByText(beta.title, { exact: true })).toBeVisible();

    // ---- 4. EXACT COUNTS + ABSENCE: each section's subtree holds exactly its own fields --------
    const alphaBody = page.locator(`[data-tc-section="${alpha.section}"]`);
    const betaBody = page.locator(`[data-tc-section="${beta.section}"]`);
    await expect(alphaBody, "the first section rendered more than once").toHaveCount(1);
    await expect(betaBody, "the second section rendered more than once").toHaveCount(1);

    await expect(alphaBody.locator("[data-tc-field]")).toHaveCount(alpha.fields.length);
    await expect(betaBody.locator("[data-tc-field]")).toHaveCount(beta.fields.length);
    for (const f of alpha.fields) {
      await expect(alphaBody.locator(`[data-tc-field="${f.fieldname}"]`)).toHaveCount(1);
      await expect(alphaBody.getByText(f.label, { exact: true })).toBeVisible();
    }
    for (const f of beta.fields) {
      await expect(betaBody.locator(`[data-tc-field="${f.fieldname}"]`)).toHaveCount(1);
      // The point of the phase: section B's field is NOT in section A's DOM subtree.
      await expect(
        alphaBody.locator(`[data-tc-field="${f.fieldname}"]`),
        `${f.fieldname} is rendered inside "${alpha.title}" as well as "${beta.title}"`,
      ).toHaveCount(0);
      await expect(alphaBody.getByText(f.label, { exact: true })).toHaveCount(0);
    }

    // Nothing escaped into the unsectioned group, and with one tab there is no strip.
    await expect(page.locator('[data-tc-section=""]'), "a declared field fell out of its section").toHaveCount(0);
    await expect(page.locator("[data-tc-tabs]"), "a tab strip rendered for a single declared tab").toHaveCount(0);
  });
});
