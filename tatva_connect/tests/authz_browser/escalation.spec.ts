import { test, expect, request as pwRequest } from "@playwright/test";
import {
  loadCreds,
  storageStateFor,
  forbiddenVerticalsFor,
  Persona,
} from "./auth.setup";

/**
 * escalation.spec.ts — ACTIVE break attempts (A1/A2/A6/A7/A10).
 *
 * Pairs an attacker grain persona against a victim grain persona in a DIFFERENT
 * vertical (grain_4 vs grain_5, which share program "InsideSales" — the A2
 * trap). The victim's own session is used ONLY to discover a real lead name in
 * the victim's grain; every actual attack is performed AS THE ATTACKER.
 *
 * Attacks:
 *   (a) direct-navigate the attacker's browser to the victim lead URL
 *       (/crm/leads/<name>) → assert access denied / no victim data shown.
 *   (b) call a guarded API method against the victim row as the attacker
 *       (page.request, carrying the attacker's session) → assert non-2xx/empty.
 *   (c) read a permlevel-1 field of the victim row via /api/resource
 *       → assert the field is not returned.
 */

const creds: Persona[] = loadCreds();
const byVertical = (v: string) =>
  creds.find((c) => (c.grain_key || "").split("::")[0] === v);

const attacker = byVertical("TatvaPractice"); // grain_4
const victim = byVertical("GoodFlip") || byVertical("GoodFlip Care"); // grain_5

const havePair = Boolean(attacker && victim);

test.describe("active escalation — grain_4 attacks grain_5", () => {
  test.skip(!havePair, "need both grain_4 and grain_5 personas in creds");

  // Discover a real victim lead name using the VICTIM's own session.
  async function findVictimLeadName(baseURL: string): Promise<string | null> {
    const ctx = await pwRequest.newContext({
      baseURL,
      storageState: storageStateFor(victim!.persona),
    });
    try {
      const resp = await ctx.get(
        "/api/method/frappe.client.get_list" +
          "?doctype=CRM%20Lead&fields=[\"name\"]&limit_page_length=1",
      );
      if (!resp.ok()) return null;
      const payload = await resp.json().catch(() => ({}));
      const rows: Array<{ name?: string }> = payload?.message || [];
      return rows[0]?.name ?? null;
    } finally {
      await ctx.dispose();
    }
  }

  test("(a) URL-tamper: attacker cannot view victim lead page", async ({
    browser,
    baseURL,
  }) => {
    const base = baseURL || "http://dev.localhost:8000";
    const victimLead = await findVictimLeadName(base);
    test.skip(!victimLead, "no victim lead available to attack");

    const context = await browser.newContext({
      baseURL: base,
      storageState: storageStateFor(attacker!.persona),
    });
    const page = await context.newPage();
    try {
      await page.goto(`/crm/leads/${victimLead}`, {
        waitUntil: "networkidle",
      });
      await page
        .waitForLoadState("networkidle", { timeout: 12000 })
        .catch(() => undefined);

      const bodyText = (await page.locator("body").innerText()).toString();

      // The attacker must NOT see the victim vertical anywhere on the page.
      const forbidden = forbiddenVerticalsFor(attacker!.grain_key);
      for (const vertical of forbidden) {
        expect(
          bodyText,
          `attacker ${attacker!.persona} saw victim vertical "${vertical}" ` +
            `on /crm/leads/${victimLead} — horizontal LEAK`,
        ).not.toContain(vertical);
      }

      // A denied/not-found state is the expected shape. We assert at least one
      // denial signal is present (any of these), OR the page rendered no lead
      // detail at all. Tighten the denial selector once confirmed live.
      const deniedSignals = [
        /not permitted/i,
        /no permission/i,
        /denied/i,
        /not found/i,
        /does(?:n['’]| no)t exist/i,
        /403|404/,
      ];
      const looksDenied = deniedSignals.some((re) => re.test(bodyText));
      const looksEmpty = bodyText.trim().length < 40;
      expect(
        looksDenied || looksEmpty,
        `attacker page for victim lead ${victimLead} neither showed a denial ` +
          `nor was empty — confirm it is not leaking victim data`,
      ).toBe(true);
    } finally {
      await context.close();
    }
  });

  test("(b) guarded method on victim row returns non-2xx / empty", async ({
    baseURL,
  }) => {
    const base = baseURL || "http://dev.localhost:8000";
    const victimLead = await findVictimLeadName(base);
    test.skip(!victimLead, "no victim lead available to attack");

    const ctx = await pwRequest.newContext({
      baseURL: base,
      storageState: storageStateFor(attacker!.persona),
    });
    try {
      // frappe.client.get is permission-checked; an out-of-grain row must be
      // denied (non-2xx) or come back empty.
      const resp = await ctx.post("/api/method/frappe.client.get", {
        data: { doctype: "CRM Lead", name: victimLead },
      });
      if (resp.ok()) {
        const payload = await resp.json().catch(() => ({}));
        const msg = payload?.message;
        expect(
          !msg || Object.keys(msg).length === 0,
          `attacker ${attacker!.persona} fetched victim lead ${victimLead} ` +
            `via frappe.client.get with a non-empty body — escalation`,
        ).toBe(true);
      } else {
        expect(resp.status(), "expected a denial status").toBeGreaterThanOrEqual(
          400,
        );
      }
    } finally {
      await ctx.dispose();
    }
  });

  test("(c) permlevel-1 field is not returned for victim row", async ({
    baseURL,
  }) => {
    const base = baseURL || "http://dev.localhost:8000";
    const victimLead = await findVictimLeadName(base);
    test.skip(!victimLead, "no victim lead available to attack");

    const ctx = await pwRequest.newContext({
      baseURL: base,
      storageState: storageStateFor(attacker!.persona),
    });
    try {
      const resp = await ctx.get(
        `/api/resource/CRM%20Lead/${encodeURIComponent(victimLead!)}` +
          '?fields=["custom_vertical"]',
      );
      // Non-2xx is a perfectly valid denial.
      if (!resp.ok()) {
        expect(resp.status()).toBeGreaterThanOrEqual(400);
        return;
      }
      const payload = await resp.json().catch(() => ({}));
      const data = payload?.data ?? payload?.message ?? {};
      const leaked =
        data &&
        Object.prototype.hasOwnProperty.call(data, "custom_vertical") &&
        data.custom_vertical != null &&
        String(data.custom_vertical).length > 0;
      expect(
        leaked,
        `permlevel-1 field custom_vertical leaked for victim lead ` +
          `${victimLead} to attacker ${attacker!.persona}`,
      ).toBe(false);
    } finally {
      await ctx.dispose();
    }
  });
});
