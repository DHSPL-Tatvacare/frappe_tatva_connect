import { test, expect, Page } from "@playwright/test";
import { loadCreds, storageStateFor, Persona } from "./harness";

/**
 * activity_fields.spec.ts — Phase 4 of
 * docs/plans/task-form-layer/2026-07-25-task-slots-to-sections-and-form-layer.md, in the real SPA.
 *
 * Before Phase 4 an activity field that named none of the 9 promoted CRM Task columns lived in the
 * JSON payload: the catalog marked it `filterable=0, sortable=0, surface="detail"`, so the SPA's
 * Filter control (which lists ONLY `filterable` catalog rows — SmartViewList.vue `filterFields`)
 * never offered it, and the composer refused an ad-hoc filter on it outright
 * ("<field> cannot be filtered on here."). Phase 4 routes every declared field through
 * `activity.api.field_target`, so the answer is a real column somewhere and the whole surface follows.
 *
 * Per plan §12 a rendered element is NOT a pass. This asserts, in order:
 *   1. VALUES — the cell text equals the exact string the fixture wrote;
 *   2. A DELTA — the rendered row count goes from an exact 2 to an exact 1 under the filter;
 *   3. ABSENCE where absence is the point — the non-matching row leaves the DOM;
 *   4. SERVER TRUTH — an API re-read agrees with the screen.
 * Plus the property the phase exists for: the previously payload-only field is OFFERED by the Filter
 * control at all, which is the option list changing from absent to present.
 *
 * FIXTURE (must exist on the target site, committed — a browser cannot see an open transaction):
 *   a CRM Task Type "ZZ Phase4 Browser Probe" declaring
 *     · `zz_b_outcome` -> target `custom_outcome`  (rule 2, a retained common CRM Task column)
 *     · `zz_b_remark`  -> no target                (rule 3, the JSON payload yesterday, an answer row today)
 *   two saved activities carrying "ZZ alpha browser" / "ZZ beta browser", and a standard Activity
 *   Smart View labelled "ZZ Phase 4 Browser View" whose saved columns are the outcome field only.
 * The spec fails loudly (not silently green) when that fixture is absent.
 *
 * Harness: the config and `auth.setup.ts` in this folder. No new runner, no new login helper.
 */

const VIEW_LABEL = "ZZ Phase 4 Browser View";
const OUTCOME_LABEL = "ZZ Browser Outcome";
const REMARK_LABEL = "ZZ Browser Remark"; // the previously payload-only field
const REMARK_KEY = "activity:zz_b_remark";
const ALPHA = "ZZ alpha browser";
const BETA = "ZZ beta browser";

// The persona the lane provisioned. AUTHZ_PERSONA picks one by name; otherwise the first in the
// creds file — the same file `auth.setup.ts` logs every persona in from.
const creds: Persona[] = loadCreds();
const persona: Persona =
  creds.find((c) => c.persona === process.env.AUTHZ_PERSONA) || creds[0];

/** One whitelisted read through the page's own session (same cookies the SPA uses). */
async function api(page: Page, method: string, params: Record<string, string> = {}) {
  const qs = new URLSearchParams(params).toString();
  const res = await page.request.get(`/api/method/${method}${qs ? `?${qs}` : ""}`);
  expect(res.ok(), `${method} returned ${res.status()}`).toBeTruthy();
  return (await res.json()).message;
}

test.describe(`activity fields are queryable — ${persona.persona}`, () => {
  // Desktop width on purpose: below it the SPA's sidebar overlays the list toolbar and swallows the
  // click on the editor button — a layout fact, not a defect under test.
  test.use({
    storageState: storageStateFor(persona.persona),
    viewport: { width: 1680, height: 1000 },
  });

  test("a former payload field is a column, a filter, and narrows 2 rows to 1", async ({ page }) => {
    // ---- the fixture, and the baseline the delta is measured from ---------------------------
    const tabs = await api(page, "tatva_connect.smartview.api.get_smart_views");
    const tab = (tabs || []).find((t: { label: string }) => t.label === VIEW_LABEL);
    expect(tab, `the fixture view "${VIEW_LABEL}" is not on this site`).toBeTruthy();
    const view: string = tab.name;

    const catalog = await api(page, "tatva_connect.smartview.api.field_catalog", {
      base_object: "Activity",
      activity_type: tab.activity_type,
    });
    const remark = (catalog || []).find((c: { label: string }) => c.label === REMARK_LABEL);
    expect(remark, `${REMARK_LABEL} is not in the catalog`).toBeTruthy();
    // The phase, stated as the catalog states it: not a promoted CRM Task column, and still queryable.
    expect(remark.sql_source, "the remark should resolve to a section row, not the task row").not.toBe("task");
    expect(remark.filterable, `${REMARK_LABEL} is still not filterable`).toBeTruthy();
    expect(remark.sortable, `${REMARK_LABEL} is still not sortable`).toBeTruthy();

    const before = await api(page, "tatva_connect.smartview.api.get_data", {
      view,
      page_size: "50",
    });
    expect(before.total, "the fixture view must hold exactly the two probe activities").toBe(2);

    // The PWA service worker caches aggressively (plan §12) — cache-bust before trusting a screen.
    await page.goto(`/crm/smart-views/${view}?cb=${Date.now()}`, { waitUntil: "networkidle" });
    await expect(page.getByText(OUTCOME_LABEL, { exact: true })).toBeVisible();
    // Stock CRM's onboarding Help panel (AppSidebar.vue `HelpModal`) auto-opens as a fixed right-hand
    // overlay on a fresh session and swallows every click on the list toolbar. Close it if it is up.
    const helpPanel = page.locator("div.fixed.z-50.right-0");
    if (await helpPanel.count()) {
      await helpPanel.locator("button:has(svg.feather-x)").first().click();
      await expect(helpPanel).toHaveCount(0);
    }

    // ---- 1. the column picker offers it, and adding it renders the fixture's own values -------
    // The editor entry point is an icon Button whose label is a TOOLTIP, so it has no accessible
    // name — it is addressed by its icon inside the list toolbar (SmartViewList.vue: `icon="edit-2"`).
    // Scoped to the toolbar that holds Filter: the sidebar carries an edit-2 icon of its own.
    const toolbar = page
      .locator("div.ml-auto")
      .filter({ has: page.getByRole("button", { name: "Filter", exact: true }) });
    await toolbar.locator("button:has(svg.feather-edit-2)").click();
    await page.getByRole("button", { name: /Columns/ }).click();
    const remarkOption = page.getByRole("checkbox", { name: REMARK_LABEL });
    await expect(remarkOption, `the column picker does not offer ${REMARK_LABEL}`).toBeVisible();
    if (!(await remarkOption.isChecked())) await remarkOption.check();
    await page.getByRole("button", { name: "Save changes" }).click();
    await expect(page.getByRole("heading", { name: "Edit Smart View" })).toHaveCount(0);

    // The grid now carries the column, once — the editor's two copies of the label are gone with it.
    await expect(page.getByText(REMARK_LABEL, { exact: true })).toHaveCount(1);
    // VALUES, not presence: the cell text IS the string the fixture wrote.
    await expect(page.getByText(ALPHA, { exact: true })).toHaveCount(1);
    await expect(page.getByText(BETA, { exact: true })).toHaveCount(1);

    // ---- 2. the Filter control OFFERS it (the option list absent -> present) ------------------
    await page.getByRole("button", { name: "Filter", exact: true }).click();
    await page.getByRole("button", { name: "Add Filter" }).click();
    const option = page.getByRole("option", { name: REMARK_LABEL, exact: true });
    await expect(option, `the Filter control does not offer ${REMARK_LABEL}`).toBeVisible();
    await option.click();

    // ---- 3. and filtering on it narrows the rendered grid from an exact 2 to an exact 1 -------
    const value = page.getByRole("textbox", { name: "%John%" });
    await value.fill(`%${ALPHA}%`);
    await value.press("Enter");

    await expect(page.getByText(BETA, { exact: true }), "the non-matching row is still rendered").toHaveCount(0);
    await expect(page.getByText(ALPHA, { exact: true })).toHaveCount(1);

    // ---- 4. server truth: the database agrees with the screen ---------------------------------
    const after = await api(page, "tatva_connect.smartview.api.get_data", {
      view,
      page_size: "50",
      filters: JSON.stringify([[REMARK_KEY, "like", ALPHA]]),
    });
    expect(after.total, "the count did not narrow with the rows").toBe(1);
    expect(after.rows.map((r: Record<string, string>) => r[REMARK_KEY])).toEqual([ALPHA]);
  });
});
