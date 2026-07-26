import { test, expect, Page } from "@playwright/test";
import { loadCreds, storageStateFor, Persona } from "./harness";

/**
 * bulk_complete.spec.ts — Phase 11(a) of
 * docs/plans/task-form-layer/2026-07-25-task-slots-to-sections-and-form-layer.md (= W11 §6), in the real SPA.
 *
 * A `CRM Task Type` carrying `disable_bulk_complete` must not be completable from the task list's bulk
 * action. The flag has existed since Phase 1 and nothing read it: an operator could tick it and a rep could
 * still close twenty visits without opening one form.
 *
 * WHAT "BULK COMPLETE" IS IN THIS PRODUCT. There is no button called that. The list's bulk lane is
 * `ListSelectBanner` -> ⋯ -> **Edit** -> `EditValueModal` (title "Bulk Edit") -> Field = Status, Value =
 * Done -> "Update N Records", which calls Frappe's own
 * `bulk_update.submit_cancel_or_update_docs`. That endpoint is overridden to
 * `tatva_connect.tasks.tasks.submit_cancel_or_update_docs`, which refuses the whole call.
 *
 * WHY THE "STILL NOT DONE" ASSERTION IS NOT ENOUGH ON ITS OWN, and this matters (plan §12: a green run
 * against old code means the spec tests nothing). Core's `_bulk_action` wraps every per-document save in
 * `except Exception: log_error(); failed.append(name)`. On the OLD code, if any `validate` guard happened to
 * refuse these saves, the tasks would ALSO stay open — while the rep saw a success toast. So the
 * discriminating assertions are the two that can only be true after the change:
 *   · an ERROR is shown and its text NAMES THE TYPE (nothing on the old path names a type at all);
 *   · the Bulk Edit dialog is STILL OPEN — the old path closes it, toasts success and reloads the list.
 * The status re-read is the third leg, asserted through the API as SERVER TRUTH after the fact.
 *
 * FIXTURE (must exist on the target site, COMMITTED — a browser cannot see an open transaction). The lead
 * and the type sit on the RUNNING PERSONA'S OWN GRAIN (`grain_key` in `authz_creds.json`, "vertical::group::
 * program"): a grain persona cannot read a lead outside it, and the task list is scoped by the lead. The
 * tasks are ASSIGNED to the persona so they are on the list they are being selected from. The type declares
 * one schema field so it is a real activity type and `list_types_for_lead` offers it — that is where this
 * spec reads the type's clean LABEL from, rather than hardcoding a spelling.
 * In `bench --site <site> console`:
 *
 *   import frappe
 *   V, G, P = "<vertical>", "<group>", "<program>"     # from authz_creds.json grain_key
 *   PERSONA = "<persona email>"                        # from authz_creds.json
 *   t = frappe.get_doc({"doctype": "CRM Task Type", "type_name": "ZZ Phase11 Bulk Blocked",
 *       "vertical": V, "group": G, "program": P, "disable_bulk_complete": 1, "schema": [
 *           {"label": "ZZ P11 Note", "fieldname": "zz_p11_note", "fieldtype": "Data"},
 *       ]}).insert(ignore_permissions=True)
 *   lead = frappe.get_doc({"doctype": "CRM Lead", "first_name": "ZZ Phase11 Bulk Lead",
 *       "mobile_no": "+919812600011", "custom_vertical": V, "custom_group": G,
 *       "custom_current_program": P}).insert(ignore_permissions=True)
 *   for i in range(3):
 *       frappe.get_doc({"doctype": "CRM Task", "title": f"ZZ Phase11 bulk probe {i}", "status": "Todo",
 *           "custom_task_type": t.name, "assigned_to": PERSONA,
 *           "reference_doctype": "CRM Lead", "reference_docname": lead.name}).insert(ignore_permissions=True)
 *   frappe.db.commit()
 *
 * TEARDOWN (leaving these behind fails the NEXT run — a lead dedupes on (mobile_no, vertical, group)):
 *   for n in frappe.get_all("CRM Task", filters={"title": ["like", "ZZ Phase11 bulk probe%"]}, pluck="name"):
 *       frappe.delete_doc("CRM Task", n, force=True, ignore_permissions=True)
 *   for n in frappe.get_all("CRM Lead", filters={"lead_name": ["like", "ZZ Phase11 Bulk Lead%"]}, pluck="name"):
 *       frappe.delete_doc("CRM Lead", n, force=True, ignore_permissions=True)
 *   frappe.delete_doc("CRM Task Type", t.name, force=True, ignore_permissions=True)
 *   frappe.db.commit()
 *
 * NOTE the stored `type_name` is TITLE-CASED by `taxonomy/normalize.py` on save, so the label reads
 * "Zz Phase11 Bulk Blocked". It is read off the server here and never hardcoded.
 *
 * Harness: `./harness` + the `playwright.config.ts` in this folder. No new runner, no new login helper.
 */

const TYPE_NAME = "ZZ Phase11 Bulk Blocked";
const LEAD_FIRST_NAME = "ZZ Phase11 Bulk Lead";
const TASK_TITLES = [
  "ZZ Phase11 bulk probe 0",
  "ZZ Phase11 bulk probe 1",
  "ZZ Phase11 bulk probe 2",
];

const creds: Persona[] = loadCreds();
const persona: Persona =
  creds.find((c) => c.persona === process.env.AUTHZ_PERSONA) || creds[0];

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

/** The three probe tasks as the SERVER holds them: [{name, status, custom_task_type}]. */
async function probeTasks(page: Page) {
  return await api(page, "frappe.client.get_list", {
    doctype: "CRM Task",
    filters: JSON.stringify([["title", "like", "ZZ Phase11 bulk probe%"]]),
    fields: JSON.stringify(["name", "title", "status", "custom_task_type"]),
    order_by: "title asc",
    limit_page_length: "10",
  });
}

/** Tick one row's checkbox by the title in its first cell. The row's own click opens the task modal, so
 *  the checkbox — which stops propagation itself — is the only thing that may be clicked. */
async function selectRow(page: Page, title: string) {
  const cell = page.getByText(title, { exact: true });
  await expect(cell, `the task "${title}" is not on the list`).toHaveCount(1);
  const row = cell.locator('xpath=ancestor::div[contains(@class,"grid")][1]');
  await row.locator('input[type="checkbox"]').click();
}

test.describe(`a type that forbids bulk completion really refuses it — ${persona.persona}`, () => {
  // Desktop width on purpose: below it the SPA's sidebar overlays the list toolbar and the select banner.
  test.use({
    storageState: storageStateFor(persona.persona),
    viewport: { width: 1680, height: 1000 },
  });

  test("bulk-completing three tasks of the type is refused by name and all three stay open", async ({
    page,
  }) => {
    // ---- 1. SERVER TRUTH FIRST: the fixture is what this spec thinks it is ----------------------
    const tasks = await probeTasks(page);
    expect(
      tasks?.length,
      "the three probe tasks are not readable on this site — see the fixture recipe in this file's header",
    ).toBe(3);
    expect(
      tasks.map((t: { status: string }) => t.status),
      "the probe tasks must start open, or 'they stayed open' proves nothing",
    ).toEqual(["Todo", "Todo", "Todo"]);
    const typeKey: string = tasks[0].custom_task_type;
    expect(
      tasks.every((t: { custom_task_type: string }) => t.custom_task_type === typeKey),
      "the three probe tasks are not all of the same type",
    ).toBeTruthy();

    const leads = await api(page, "frappe.client.get_list", {
      doctype: "CRM Lead",
      filters: JSON.stringify([["first_name", "=", LEAD_FIRST_NAME]]),
      fields: JSON.stringify(["name"]),
      limit_page_length: "1",
    });
    expect(leads?.length, `the fixture lead "${LEAD_FIRST_NAME}" is not readable`).toBe(1);

    // The clean label the refusal must carry, read off the same brain the server names it with — the
    // composite `::` primary key must never reach a rep-facing sentence.
    const offered = await api(page, "tatva_connect.activity.api.list_types_for_lead", {
      lead: leads[0].name,
    });
    const probeType = (offered || []).find(
      (t: { name: string }) => t.name === typeKey,
    );
    expect(
      probeType,
      `the fixture type "${TYPE_NAME}" is not offered for the fixture lead`,
    ).toBeTruthy();
    const typeLabel: string = probeType.label;
    expect(typeLabel, "the type has no clean label to name in a refusal").toBeTruthy();
    expect(typeLabel, "the label is the composite key, not a name").not.toContain("::");

    // ---- 2. drive the real list ----------------------------------------------------------------
    // The PWA service worker caches aggressively (plan §12) — cache-bust before trusting a screen.
    await page.goto(`/crm/tasks/view/list?cb=${Date.now()}`, {
      waitUntil: "networkidle",
    });
    // Stock CRM's onboarding Help panel auto-opens as a fixed overlay and swallows clicks. Close it.
    const helpPanel = page.locator("div.fixed.z-50.right-0");
    if (await helpPanel.count()) {
      await helpPanel.locator("button:has(svg.feather-x)").first().click();
      await expect(helpPanel).toHaveCount(0);
    }

    // ---- 3. A DELTA: an exact 3 selected, counted by the product's own banner -------------------
    for (const title of TASK_TITLES) await selectRow(page, title);
    await expect(
      page.getByText("3 rows selected", { exact: true }),
      "the three probe rows were not all selected, so the bulk action below would act on the wrong set",
    ).toBeVisible();

    // ---- 4. run the bulk lane: ⋯ -> Edit -> Status = Done -> Update 3 Records -------------------
    const banner = page
      .locator("div.absolute")
      .filter({ hasText: "3 rows selected" });
    await banner.locator("button:has(svg.feather-more-horizontal)").click();
    await page.getByRole("menuitem", { name: "Edit", exact: true }).click();

    const dialog = page.getByRole("dialog").filter({ hasText: "Bulk Edit" });
    await expect(dialog).toBeVisible();
    // The field picker is `components/frappe-ui/Autocomplete.vue`: its "placeholder" is TEXT inside the
    // trigger button, not an input placeholder, and its options are ComboboxOptions (role=option) rendered
    // in a popover outside the dialog — hence the button-by-text open and the page-level option pick.
    await dialog.locator("button").filter({ hasText: "Source" }).click();
    // The list is sliced to `maxOptions: 20` (Autocomplete.vue), so "Status" is only reliably rendered
    // once the search narrows it. `.last()` picks the popover's own ComboboxInput — it mounts after the
    // list's own search box, so it is the last "Search" field on the page.
    await page.getByPlaceholder("Search").last().fill("Status");
    await page.getByRole("option", { name: "Status", exact: true }).click();
    await dialog.locator("select").selectOption("Done");
    await page.getByRole("button", { name: "Update 3 Records" }).click();

    // ---- 5. THE REFUSAL, and it NAMES THE TYPE -------------------------------------------------
    // Only the server produces this sentence, and only the server knows the type's label — the client has
    // no rule about `disable_bulk_complete` at all, so this cannot have come from the paint.
    await expect(
      page.getByText(new RegExp(typeLabel.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"))).first(),
      "the bulk completion was not refused with a message naming the type",
    ).toBeVisible();
    // ABSENCE where absence is the point: the success path closes the dialog and toasts nothing else.
    await expect(
      dialog,
      "the Bulk Edit dialog closed, which is what the SUCCESS path does — the call was not refused",
    ).toBeVisible();

    // ---- 6. SERVER TRUTH AFTER THE FACT: not one of the three moved -----------------------------
    const after = await probeTasks(page);
    expect(
      after.map((t: { status: string }) => t.status),
      "a task was completed anyway; the refusal must leave the WHOLE selection untouched",
    ).toEqual(["Todo", "Todo", "Todo"]);
  });
});
