import { test, expect, Page } from "@playwright/test";
import { loadCreds, storageStateFor, Persona } from "./auth.setup";

/**
 * spotlight.spec.ts — the spotlight's honest index status (plan P3).
 *
 * The endpoint used to answer `{results: [], total: 0}` whether it was dormant,
 * mid-build, or genuinely matching nothing — three different situations the user
 * reads as one broken search. It now carries `status`: disabled | building | ready.
 *
 * Per the plan's §8 criteria, a rendered element is NOT a pass. Every assertion
 * below is one of:
 *   · NETWORK PAYLOAD  — the intercepted search response body
 *   · COUNT AGREEMENT  — rendered rows vs the payload's own count
 *   · NEGATIVE / AUTHZ — no returned lead outside the persona's own list scope
 *   · STATE TRANSITION — same query, building ⇒ prepared-text, ready ⇒ rows
 *
 * The state-transition block drives the REAL component with the endpoint's real
 * response shape supplied by `page.route`. It asserts the CLIENT contract only —
 * that a `building` index is not rendered as "No results". That the SERVER really
 * says `building` when the index is absent or cut short is proven separately, and
 * against a real index file, by tatva_connect/tests/search/test_search_status.py.
 */

const SEARCH_RE = /tatva_connect\.search\.api\.search/;
const PLACEHOLDER = "Search leads, notes, files";
const BUILDING_TEXT = "Search is still being prepared";
const NO_RESULTS_TEXT = "No results for";
const STATUSES = ["disabled", "building", "ready"];

const creds: Persona[] = loadCreds();
const grainPersonas = creds.filter((c) => (c.grain_key || "").includes("::"));

/** Open the spotlight (⌘K / Ctrl+K) and return its input. */
async function openSpotlight(page: Page) {
  await page.goto("/crm/leads", { waitUntil: "networkidle" });
  await page.keyboard.press("ControlOrMeta+k");
  const input = page.getByPlaceholder(PLACEHOLDER);
  await expect(input).toBeVisible();
  return input;
}

/** Type a query and return the parsed body of the search response it triggers. */
async function searchAndCapture(page: Page, term: string) {
  const waiter = page.waitForResponse(
    (r) => SEARCH_RE.test(r.url()) && r.request().method() !== "OPTIONS",
  );
  await page.getByPlaceholder(PLACEHOLDER).fill(term);
  const resp = await waiter;
  const body = await resp.json().catch(() => ({}));
  return body?.message ?? {};
}

/** A search term taken from the persona's OWN data — nothing hardcoded about the corpus. */
async function termFromOwnLeads(request: any): Promise<string | null> {
  const resp = await request.get(
    "/api/method/frappe.client.get_list?doctype=CRM%20Lead" +
      '&fields=["name","lead_name"]&limit_page_length=20',
  );
  if (!resp.ok()) return null;
  const rows: Array<Record<string, unknown>> = (await resp.json())?.message || [];
  for (const row of rows) {
    const token = String(row?.lead_name ?? "")
      .split(/\s+/)
      .find((t) => t.length >= 4 && /^[A-Za-z]+$/.test(t));
    if (token) return token;
  }
  return null;
}

for (const cred of grainPersonas) {
  test.describe(`spotlight status — ${cred.persona} (${cred.grain_key})`, () => {
    test.use({ storageState: storageStateFor(cred.persona) });

    test("NETWORK PAYLOAD: every search response carries a known status", async ({
      page,
      request,
    }) => {
      const term = (await termFromOwnLeads(request)) || "kum";
      await openSpotlight(page);
      const body = await searchAndCapture(page, term);

      expect(
        body,
        "the search endpoint must return a `status` on every path — an empty " +
          "list with no status is indistinguishable from a broken search",
      ).toHaveProperty("status");
      expect(STATUSES).toContain(body.status);
    });

    test("NETWORK PAYLOAD: the short-query early return still carries a status", async ({
      page,
    }) => {
      // Below the 3-char floor the endpoint returns before searching; the UI still needs to know why.
      await openSpotlight(page);
      const body = await page
        .evaluate(async () => {
          const r = await fetch(
            "/api/method/tatva_connect.search.api.search?query=ka",
            { headers: { Accept: "application/json" } },
          );
          return (await r.json())?.message;
        })
        .catch(() => null);
      expect(body).toHaveProperty("status");
      expect(STATUSES).toContain(body.status);
    });

    test("COUNT AGREEMENT: rendered rows equal the payload's own result count", async ({
      page,
      request,
    }) => {
      const term = (await termFromOwnLeads(request)) || "kum";
      await openSpotlight(page);
      const body = await searchAndCapture(page, term);
      test.skip(body.status !== "ready", `index status is "${body.status}", not ready`);

      const expected = (body.results || []).length;
      await expect(page.locator("li:has(button)")).toHaveCount(expected, { timeout: 10_000 });
    });

    test("NEGATIVE / AUTHZ: no hit references a lead outside the persona's own scope", async ({
      page,
      request,
    }) => {
      // Rows are pre-filtered by principal and then gated authoritatively by get_list; this proves it on the wire.
      const term = (await termFromOwnLeads(request)) || "kum";
      const listed = await request.get(
        "/api/method/frappe.client.get_list?doctype=CRM%20Lead" +
          '&fields=["name"]&limit_page_length=0',
      );
      test.skip(!listed.ok(), "lead list API unavailable for this persona");
      const visible = new Set<string>(
        ((await listed.json())?.message || []).map((r: any) => String(r.name)),
      );

      await openSpotlight(page);
      const body = await searchAndCapture(page, term);
      for (const hit of body.results || []) {
        if (!hit?.lead) continue;
        expect(
          visible.has(String(hit.lead)),
          `persona ${cred.persona} got a spotlight hit on lead ${hit.lead}, ` +
            `which is NOT in that persona's own lead list — index scope LEAK`,
        ).toBe(true);
      }
    });
  });
}

/**
 * STATE TRANSITION — the same query, three server states, driven through the real
 * component. The response bodies are the endpoint's real shape; the server-side
 * derivation of each state is proven in the Python suite against a real index.
 */
test.describe("spotlight status — client contract for each state", () => {
  test.use({ storageState: storageStateFor((grainPersonas[0] || creds[0]).persona) });

  async function stubStatus(page: Page, message: Record<string, unknown>) {
    await page.route(SEARCH_RE, (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ message }),
      }),
    );
  }

  test("building ⇒ prepared text; ready ⇒ rows; same query throughout", async ({ page }) => {
    const term = "kavita";
    await stubStatus(page, { results: [], total: 0, status: "building" });
    const input = await openSpotlight(page);
    await input.fill(term);
    await expect(page.getByText(BUILDING_TEXT)).toBeVisible();
    await expect(page.getByText(NO_RESULTS_TEXT)).toHaveCount(0);

    // Same query, index now built: the row appears and the prepared text goes away.
    await page.unroute(SEARCH_RE);
    await stubStatus(page, {
      status: "ready",
      total: 1,
      results: [
        {
          doctype: "CRM Lead",
          name: "LEAD-STUB-0001",
          lead: "LEAD-STUB-0001",
          tab: null,
          title: term,
          snippet: "",
          score: 1,
        },
      ],
    });
    await input.fill("");
    await input.fill(term);
    await expect(page.getByText(BUILDING_TEXT)).toHaveCount(0);
    await expect(page.locator("li:has(button)")).toHaveCount(1);
  });

  test("ready + zero results says No results, never 'being prepared'", async ({ page }) => {
    // The lazy implementation this whole key exists to prevent: no hits is NOT not-ready.
    await stubStatus(page, { results: [], total: 0, status: "ready" });
    const input = await openSpotlight(page);
    await input.fill("zzqxvwmatchesnothing");
    await expect(page.getByText(NO_RESULTS_TEXT)).toBeVisible();
    await expect(page.getByText(BUILDING_TEXT)).toHaveCount(0);
  });

  test("disabled renders exactly as today — the toggle is never surfaced", async ({ page }) => {
    // Dormant by default: a user must not be told about an internal operator switch.
    await stubStatus(page, { results: [], total: 0, status: "disabled" });
    const input = await openSpotlight(page);
    await input.fill("kavita");
    await expect(page.getByText(NO_RESULTS_TEXT)).toBeVisible();
    await expect(page.getByText(BUILDING_TEXT)).toHaveCount(0);
  });
});
