import { test, expect, Page } from "@playwright/test";
import { loadCreds, Persona } from "./harness";

/**
 * modal_probe.spec.ts — a MEASUREMENT, not an assertion suite.
 *
 * It opens the task modal the way a rep does — from the LEAD, which is the only path whose card carries
 * `custom_task_type` (api/activities.py:199) — and prints every server call each click generates, plus
 * the timing that decides whether the type picker is usable the moment the form appears.
 *
 * READ ONLY: never clicks Save, never creates a row, so it does not need the comms-off interlock the
 * authz specs run behind. Run with `--no-deps` so `auth.setup.ts` (and its interlock) is skipped.
 *
 *   AUTHZ_BASE_URL=http://dev.localhost:8080 PROBE_PERSONA=sales_manager \
 *     npx playwright test modal_probe.spec.ts --no-deps --reporter=list
 */

const creds: Persona[] = loadCreds();
const persona: Persona =
  creds.find((c) => c.persona === process.env.PROBE_PERSONA) || creds[0];

type Call = { phase: string; method: string; at: number; answeredAt?: number; lead?: string | null };

/** `/api/method/a.b.c?x=1` -> `a.b.c`. */
function methodName(url: string): string {
  const m = url.match(/\/api\/method\/([^?]+)/);
  return m ? decodeURIComponent(m[1]) : url;
}

/** The `lead` a call names — query string for a GET, JSON body for a POST (frappe-ui uses both). */
function leadParam(r: { url(): string; postData(): string | null }): string | null {
  const q = new URL(r.url()).searchParams.get("lead");
  if (q !== null) return q;
  const body = r.postData();
  if (!body) return null;
  try {
    const v = JSON.parse(body).lead;
    return v === undefined ? null : String(v);
  } catch {
    return new URLSearchParams(body).get("lead");
  }
}

/** A phase ends when the network has been quiet for `quietMs` — SPA websockets never let it go idle. */
async function settle(page: Page, quietMs = 1200, capMs = 10000) {
  const started = Date.now();
  let lastSeen = Date.now();
  const bump = () => (lastSeen = Date.now());
  page.on("request", bump);
  while (Date.now() - lastSeen < quietMs && Date.now() - started < capMs) {
    await page.waitForTimeout(150);
  }
  page.off("request", bump);
}

test.describe(`task modal paint probe — ${persona.persona}`, () => {
  test.use({ viewport: { width: 1680, height: 1000 } });

  test("count the calls each click makes, and when the type list lands", async ({ page }) => {
    const login = await page.request.post("/api/method/login", {
      data: { usr: persona.email, pwd: persona.password },
    });
    expect(login.ok(), `login failed for ${persona.persona}`).toBeTruthy();

    // Pinned, not discovered: an entitled persona's `get_list` on CRM Task is grain-scoped and answers
    // nothing, so the lead is named and the first card on its Tasks tab is the task.
    const lead: string = process.env.PROBE_LEAD || "6bfh3qdqep";

    const calls: Call[] = [];
    let phase = "lead page load";
    page.on("request", (r) => {
      const u = r.url();
      if (u.includes("/api/method/"))
        calls.push({
          phase,
          method: methodName(u),
          at: Date.now(),
          lead: leadParam(r),
        });
    });
    page.on("response", (r) => {
      const u = r.url();
      if (!u.includes("/api/method/")) return;
      const name = methodName(u);
      const open = [...calls].reverse().find((c) => c.method === name && !c.answeredAt);
      if (open) open.answeredAt = Date.now();
    });

    await page.goto(`/crm/leads/${encodeURIComponent(lead)}`, { waitUntil: "domcontentloaded" });
    await page.getByRole("tab", { name: "Tasks", exact: true }).click({ timeout: 30000 });
    const card = page.locator('div[role="button"].cursor-pointer').first();
    await expect(card, `no task card on lead ${lead}`).toBeVisible({ timeout: 30000 });
    await settle(page);

    // ---- PHASE 1: open the task -------------------------------------------------------------
    phase = "open task";
    const clickedAt = Date.now();
    await card.click();
    await expect(page.locator("[data-tc-body]")).toBeVisible({ timeout: 30000 });
    const paintedAt = Date.now();
    await settle(page);
    const fieldCount = await page.locator("[data-tc-field]").count();
    const shownFields = await page.locator("[data-tc-field]:visible").count();
    await page.screenshot({ path: ".auth/modal-open.png" });

    phase = "idle (modal open)";
    await settle(page, 1500, 3000);

    await page.keyboard.press("Escape");
    await expect(page.locator("[data-tc-body]")).toHaveCount(0, { timeout: 10000 });
    await settle(page);

    // ---- PHASE 2: open it again — what a second open still costs ------------------------------
    phase = "reopen same task";
    await card.click();
    await expect(page.locator("[data-tc-body]")).toBeVisible({ timeout: 30000 });
    await settle(page);
    await page.keyboard.press("Escape");
    await expect(page.locator("[data-tc-body]")).toHaveCount(0, { timeout: 10000 });
    await settle(page);

    // ---- PHASE 3: the create form, where the type picker is live -----------------------------
    phase = "open create form";
    let pickerState = "not reached";
    let pickerReadyMs: number | null = null;
    try {
      await page
        .getByRole("button", { name: /^(Create|New Task|Create Task)$/ })
        .first()
        .click({ timeout: 8000 });
      await expect(page.locator("[data-tc-body]")).toBeVisible({ timeout: 20000 });
      const createPaintedAt = Date.now();
      const picker = page.locator("[data-tc-typepicker]");
      pickerState = (await picker.count()) ? "present" : "absent";
      await settle(page);
      const list = calls.find(
        (c) => c.method.endsWith("list_types_for_lead") && c.phase === "open create form",
      );
      if (list?.answeredAt) pickerReadyMs = list.answeredAt - createPaintedAt;
    } catch (e) {
      pickerState = `create button not found (${(e as Error).message.split("\n")[0]})`;
    }

    // ---- the report ---------------------------------------------------------------------------
    const typeList = calls.find(
      (c) => c.method.endsWith("list_types_for_lead") && c.phase === "open task",
    );
    const out: string[] = ["", "=".repeat(74), "TASK MODAL — what each click asks the server for", "=".repeat(74)];
    for (const p of [
      "lead page load",
      "open task",
      "idle (modal open)",
      "reopen same task",
      "open create form",
    ]) {
      const mine = calls.filter((c) => c.phase === p);
      const counts = new Map<string, number>();
      for (const c of mine) counts.set(c.method, (counts.get(c.method) || 0) + 1);
      out.push(`\n${p.toUpperCase()}  —  ${mine.length} call(s)`);
      if (!mine.length) out.push("   (none)");
      for (const [m, n] of [...counts.entries()].sort((a, b) => b[1] - a[1]))
        out.push(`   ${String(n).padStart(2)} x ${m}`);
    }
    out.push("", "-".repeat(74), "TIMING");
    out.push(`   open task: click -> form painted        : ${paintedAt - clickedAt} ms`);
    out.push(
      `   open task: type list vs paint          : ${
        typeList?.answeredAt
          ? `${typeList.answeredAt - paintedAt >= 0 ? "+" : ""}${typeList.answeredAt - paintedAt} ms`
          : "not asked in this phase"
      }`,
    );
    out.push(
      `   create form: type list vs paint        : ${
        pickerReadyMs === null ? "not measured" : `${pickerReadyMs >= 0 ? "+" : ""}${pickerReadyMs} ms`
      }   (+ = picker was empty that long after the form appeared)`,
    );
    out.push(`   task type picker on create form        : ${pickerState}`);
    // The `lead` a schema fetch names is the whole of finding #1 — a blank one is the wrong-lead ask.
    out.push("", "EVERY type_config ASK (phase :: lead param)");
    for (const c of calls.filter((c) => /type_config|list_types_for_lead/.test(c.method)))
      out.push(`   ${c.phase} :: lead=${c.lead ?? "(none)"}`);
    out.push(`   declared fields in DOM / visible        : ${fieldCount} / ${shownFields}`);
    out.push("=".repeat(74), "");
    console.log(out.join("\n"));
  });
});
