import { test, expect, Page } from "@playwright/test";
import { loadCreds, storageStateFor, Persona } from "./harness";

/**
 * rules_and_lead_fields.spec.ts — Phases 9 and 10 of
 * docs/plans/task-form-layer/2026-07-25-task-slots-to-sections-and-form-layer.md, in the real SPA.
 *
 * Phase 9: an admin declares the form's behaviour as flat rows on the task type; the server compiles them
 * into the `depends_on` / `mandatory_depends_on` strings the two SHIPPED evaluators already read. Phase 10:
 * a field declaring `source = Lead` is read from and written back to the LEAD, and the task keeps no copy.
 *
 * Per plan §12 a rendered element is NOT a pass. This asserts, in order:
 *   1. SERVER TRUTH FIRST — every expectation below is checked against `type_config` before the browser is
 *      driven, so a fixture that is not what this spec thinks it is fails loudly instead of proving nothing;
 *   2. EXACT COUNTS — the number of rendered fields goes from exactly N to exactly M when the Outcome is
 *      picked, and back to exactly N when it is un-picked;
 *   3. ABSENCE where absence is the point — a hidden field is not in the DOM at all, not merely invisible;
 *   4. A SERVER error, not a client block — saving with the rule-made-mandatory field empty must be refused
 *      by the SERVER and the message must name that field. The client deliberately does not pre-check a
 *      compiled mandatory, so a green here proves the server rule and not the paint;
 *   5. SERVER TRUTH AFTER THE FACT — the lead-sourced value is re-read from the LEAD through the API, and
 *      the task is re-read to prove it carries no copy of it.
 *
 * FIXTURE (must exist on the target site, COMMITTED — a browser cannot see an open transaction). The lead and
 * the type sit on the RUNNING PERSONA'S OWN GRAIN (`grain_key` in `authz_creds.json`, "vertical::group::
 * program"), because a grain persona cannot read a lead outside it and the type picker is grain-scoped. The
 * lead field also needs its grain contract tick, or the save is refused server-side — which is the point of
 * the gate, and would look like a broken spec. In `bench --site <site> console`:
 *
 *   import frappe
 *   V, G, P = "<vertical>", "<group>", "<program>"     # from authz_creds.json grain_key
 *   frappe.get_doc({"doctype": "CRM Task Type", "type_name": "ZZ Phase 9 Browser Probe",
 *       "vertical": V, "group": G, "program": P, "schema": [
 *           {"label": "ZZ P9 Outcome", "fieldname": "zz_p9_outcome", "fieldtype": "Select",
 *            "options": "Connected\nNot Connected"},
 *           {"label": "ZZ P9 Order Id", "fieldname": "zz_p9_order_id", "fieldtype": "Data"},
 *           {"label": "ZZ P9 Units", "fieldname": "zz_p9_units", "fieldtype": "Data"},
 *           {"label": "ZZ P9 Job Title", "fieldname": "job_title", "fieldtype": "Data", "source": "Lead"},
 *       ], "rules": [
 *           {"rule_label": "Form onload", "action": "Show", "targets": "zz_p9_outcome"},
 *           {"rule_label": "Form onload", "action": "Hide", "targets": "zz_p9_order_id, zz_p9_units"},
 *           {"rule_label": "Connected", "condition_field": "zz_p9_outcome", "operator": "is",
 *            "condition_value": "Connected", "action": "Show", "targets": "zz_p9_order_id, zz_p9_units"},
 *           {"rule_label": "Connected", "condition_field": "zz_p9_outcome", "operator": "is",
 *            "condition_value": "Connected", "action": "Make Mandatory", "targets": "zz_p9_order_id"},
 *           {"rule_label": "Not connected", "condition_field": "zz_p9_outcome", "operator": "is",
 *            "condition_value": "Not Connected", "action": "Hide", "targets": "zz_p9_units"},
 *       ]}).insert(ignore_permissions=True)
 *   frappe.get_doc({"doctype": "CRM Lead", "first_name": "ZZ Phase9 Browser Lead",
 *       "mobile_no": "+919812600002", "custom_vertical": V, "custom_group": G,
 *       "custom_current_program": P, "job_title": "ZZ Before"}).insert(ignore_permissions=True)
 *   # the Phase 10 write gate: a can_set catalog row + this grain's internal contract ticking its key
 *   from tatva_connect.tests.automation import field_allowlist
 *   field_allowlist.seed_settable("CRM Lead", "job_title", vertical=V, group=G, program=P)
 *   frappe.db.commit()
 *
 * NOTE the type's stored `type_name` is TITLE-CASED by `taxonomy/normalize.py` on save, so the picker offers
 * "Zz Phase 9 Browser Probe". The label is therefore read off `list_types_for_lead` and never hardcoded.
 *
 * Harness: the config and `auth.setup.ts` in this folder. No new runner, no new login helper.
 */

const TYPE_NAME = "ZZ Phase 9 Browser Probe";
const LEAD_FIRST_NAME = "ZZ Phase9 Browser Lead";

const OUTCOME = "zz_p9_outcome";
const ORDER_ID = "zz_p9_order_id";
const UNITS = "zz_p9_units";
const LEAD_FIELD = "job_title";

// The declaration this spec was written against. Re-read off type_config below, so a server that returned a
// reshuffled or empty form cannot make both sides agree on nothing.
const OPENS_WITH = [OUTCOME, LEAD_FIELD];
const CONNECTED_SHOWS = [OUTCOME, ORDER_ID, UNITS, LEAD_FIELD];

const creds: Persona[] = loadCreds();
const persona: Persona =
  creds.find((c) => c.persona === process.env.AUTHZ_PERSONA) || creds[0];

type Field = {
  fieldname: string;
  label: string;
  depends_on: string;
  mandatory_depends_on: string;
  source: string;
};

/** One whitelisted read through the page's own session (same cookies the SPA uses). */
async function api(
  page: Page,
  method: string,
  params: Record<string, string> = {},
) {
  const qs = new URLSearchParams(params).toString();
  const res = await page.request.get(
    `/api/method/${method}${qs ? `?${qs}` : ""}`,
  );
  expect(res.ok(), `${method} returned ${res.status()}`).toBeTruthy();
  return (await res.json()).message;
}

/** The fixture lead, its probe type and that type's compiled config — the server truth this spec drives. */
async function fixture(page: Page) {
  const leads = await api(page, "frappe.client.get_list", {
    doctype: "CRM Lead",
    filters: JSON.stringify([["first_name", "=", LEAD_FIRST_NAME]]),
    fields: JSON.stringify(["name"]),
    limit_page_length: "1",
  });
  expect(
    leads?.length,
    `the fixture lead "${LEAD_FIRST_NAME}" is not readable on this site`,
  ).toBe(1);
  const lead: string = leads[0].name;

  const types = await api(page, "tatva_connect.activity.api.list_types_for_lead", {
    lead,
  });
  // The stored type_name is title-cased on save, so match case-insensitively rather than assume a spelling.
  const probe = (types || []).find(
    (t: { label: string }) =>
      (t.label || "").toLowerCase() === TYPE_NAME.toLowerCase(),
  );
  expect(probe, `the fixture type "${TYPE_NAME}" is not offered for this lead`).toBeTruthy();

  const cfg = await api(page, "tatva_connect.activity.api.type_config", {
    task_type: probe.name,
    lead,
  });
  return { lead, label: probe.label as string, cfg };
}

/** Open the log-activity modal on a lead and pick the probe type. */
async function openForm(page: Page, lead: string, label: string) {
  // The PWA service worker caches aggressively (plan §12) — cache-bust before trusting a screen.
  await page.goto(`/crm/leads/${lead}?cb=${Date.now()}#tasks`, {
    waitUntil: "networkidle",
  });
  // Stock CRM's onboarding Help panel auto-opens as a fixed overlay and swallows clicks. Close it.
  const helpPanel = page.locator("div.fixed.z-50.right-0");
  if (await helpPanel.count()) {
    await helpPanel.locator("button:has(svg.feather-x)").first().click();
    await expect(helpPanel).toHaveCount(0);
  }
  // "Log Activity" is reached through the bridge TatvaTasks publishes for exactly this, so the spec drives
  // the product's own entry point without hunting a menu.
  await page.waitForFunction(
    () => typeof (window as any).__tcLogActivity === "function",
  );
  await page.evaluate(() => (window as any).__tcLogActivity());
  await page.getByRole("button", { name: label, exact: true }).click();
}

test.describe(`the activity form obeys its declared rules — ${persona.persona}`, () => {
  // Desktop width on purpose: the modal lays fields out two-up only from the `sm` breakpoint, and the
  // sidebar overlays the lead header below it.
  test.use({
    storageState: storageStateFor(persona.persona),
    viewport: { width: 1680, height: 1000 },
  });

  test("the rules reveal and hide exactly the fields they name, and the server enforces the mandatory one", async ({
    page,
  }) => {
    // ---- 1. server truth: what the type actually compiles to -----------------------------------
    const { lead, label, cfg } = await fixture(page);
    const fields: Field[] = cfg.fields || [];
    const byName = new Map(fields.map((f) => [f.fieldname, f]));

    expect(
      fields.map((f) => f.fieldname).sort(),
      "the fixture does not declare the four fields this spec was written against",
    ).toEqual([...CONNECTED_SHOWS].sort());
    // The rules compiled: three fields carry a condition, and the reveal target also carries a compiled
    // mandatory. Nothing here evaluates the expression — that is the shipped evaluator's job.
    expect(byName.get(OUTCOME)!.depends_on, "the onload Show row did not compile").toContain("1");
    for (const fieldname of [ORDER_ID, UNITS]) {
      expect(
        byName.get(fieldname)!.depends_on,
        `${fieldname} was named by a rule but carries no compiled condition`,
      ).toContain(OUTCOME);
    }
    expect(
      byName.get(ORDER_ID)!.mandatory_depends_on,
      "the Make Mandatory row did not compile onto its target",
    ).toContain(OUTCOME);
    expect(
      byName.get(LEAD_FIELD)!.depends_on,
      "the lead field is named by no rule and must carry no condition",
    ).toBe("");

    // ---- 2. drive the real form ----------------------------------------------------------------
    await openForm(page, lead, label);
    const rendered = page.locator("[data-tc-field]");

    // ---- 3. EXACT COUNT + ABSENCE: the form opens as the onload rows declare it ----------------
    await expect(
      rendered,
      "the form did not open with exactly the fields the onload rows leave shown",
    ).toHaveCount(OPENS_WITH.length);
    for (const fieldname of OPENS_WITH) {
      await expect(page.locator(`[data-tc-field="${fieldname}"]`)).toHaveCount(1);
    }
    for (const fieldname of [ORDER_ID, UNITS]) {
      await expect(
        page.locator(`[data-tc-field="${fieldname}"]`),
        `${fieldname} is in the DOM before its rule revealed it`,
      ).toHaveCount(0);
    }

    // ---- 4. THE DELTA: picking the trigger value reveals exactly the rule's targets ------------
    await page
      .locator(`[data-tc-field="${OUTCOME}"] select`)
      .selectOption("Connected");
    await expect(
      rendered,
      "picking Connected revealed a different number of fields from the one the rules name",
    ).toHaveCount(CONNECTED_SHOWS.length);
    for (const fieldname of CONNECTED_SHOWS) {
      await expect(page.locator(`[data-tc-field="${fieldname}"]`)).toHaveCount(1);
    }

    // ---- 5. A SERVER error, not a client block -------------------------------------------------
    // ORDER_ID carries reqd = 0 and is mandatory only by rule, and the client's own pre-check reads `reqd`
    // alone — so this sentence is the SERVER's wording and cannot have come from the paint.
    await page.getByRole("button", { name: "Log Activity", exact: true }).click();
    await expect(
      page.getByText(`${byName.get(ORDER_ID)!.label} is required.`).first(),
      "the rule-made-mandatory field did not block the save with the server's own message",
    ).toBeVisible();
    await expect(
      page.getByText(/^Please fill:/),
      "the CLIENT blocked the save, so this proves the paint and not the server rule",
    ).toHaveCount(0);

    // ---- 6. THE OTHER DELTA: the Hide row takes its target back out of the DOM -----------------
    await page
      .locator(`[data-tc-field="${OUTCOME}"] select`)
      .selectOption("Not Connected");
    await expect(
      page.locator(`[data-tc-field="${UNITS}"]`),
      "the Hide row did not remove its target from the DOM",
    ).toHaveCount(0);
    await expect(
      rendered,
      "un-picking the trigger did not return the form to its opening state",
    ).toHaveCount(OPENS_WITH.length);
  });

  test("a lead-sourced field opens prefilled from the lead and is written back to it", async ({
    page,
  }) => {
    // ---- 1. server truth: the field is the LEAD's, and the config carries its current value ----
    const { lead, label, cfg } = await fixture(page);
    const field = ((cfg.fields || []) as Field[]).find(
      (f) => f.fieldname === LEAD_FIELD,
    )!;
    expect(field, "the fixture declares no lead-sourced field").toBeTruthy();
    expect(field.source, "the field is not declared as the lead's").toBe("Lead");
    const before: string = (cfg.lead_values || {})[LEAD_FIELD] || "";
    expect(
      before,
      "the fixture lead carries no value for the lead field — a prefill of nothing proves nothing",
    ).not.toBe("");

    // ---- 2. VALUE: the control opens carrying the LEAD's value, not a blank --------------------
    await openForm(page, lead, label);
    const control = page.locator(`[data-tc-field="${LEAD_FIELD}"] input`);
    await expect(
      control,
      "the lead-sourced field opened blank over a filled lead",
    ).toHaveValue(before);

    // ---- 3. edit it, satisfy the rules, and save ----------------------------------------------
    const after = `ZZ After ${Date.now()}`;
    await control.fill(after);
    await page
      .locator(`[data-tc-field="${OUTCOME}"] select`)
      .selectOption("Connected");
    await page.locator(`[data-tc-field="${ORDER_ID}"] input`).fill("zz-ord-1");
    await page.getByRole("button", { name: "Log Activity", exact: true }).click();
    await expect(
      page.getByText("Task created.").first(),
      "the save did not report success — the assertions below would read a stale lead",
    ).toBeVisible();

    // ---- 4. SERVER TRUTH AFTER THE FACT: the LEAD holds the new value -------------------------
    const reread = await api(page, "frappe.client.get_value", {
      doctype: "CRM Lead",
      filters: JSON.stringify({ name: lead }),
      fieldname: JSON.stringify([LEAD_FIELD]),
    });
    expect(
      reread[LEAD_FIELD],
      "the value typed into the activity form never reached the lead",
    ).toBe(after);

    // ---- 5. and the TASK carries no copy of it (D11) -------------------------------------------
    // The board reads the shared paged endpoint, which carries the row and not its answers — so the
    // answers are asked for per task, from the one reader the modal already uses.
    const board = await api(page, "tatva_connect.api.activities.lead_activity", { lead, kind: "task" });
    const details = await Promise.all(
      (board.data || []).map((t: { name: string }) =>
        api(page, "tatva_connect.activity.api.task_detail", { task: t.name }),
      ),
    );
    const logged = details.find(
      (t: { values: Record<string, string> }) => t.values?.[ORDER_ID] === "zz-ord-1",
    );
    expect(logged, "the activity this spec just logged is not on the board").toBeTruthy();
    expect(
      logged.values[LEAD_FIELD],
      "the task reported a value for a field that lives on the lead",
    ).toBeUndefined();
  });
});
