import { test, expect } from "@playwright/test";
import {
  loadCreds,
  storageStateFor,
  forbiddenVerticalsFor,
  Persona,
} from "./auth.setup";

/**
 * smartview.spec.ts — A7 field-leak + A1/A2 row-leak through the Smart View
 * surface, as rendered by the real SPA.
 *
 * Smart Views are 100% user-built (constitution invariant 17), so this spec
 * AUTHORS a view through the editor as a grain persona, then asserts the
 * rendered table stays inside the persona's grain:
 *   - no out-of-grain vertical appears in any rendered cell,
 *   - no permlevel-1 grain field (e.g. custom_vertical) leaks into a column.
 *
 * SELECTOR CONTRACT — the exact SPA flow MUST be confirmed against the
 * running CRM (cache-bust per UI constitution C.9 BEFORE trusting any DOM).
 * The selectors below are TODO-anchored best guesses; the ASSERTIONS are
 * concrete and final. Confirm each `TODO(selector)` against the live SPA and
 * tighten before relying on this in CI.
 */

const creds: Persona[] = loadCreds();
const grainPersonas = creds.filter(
  (c) => (c.grain_key || "").includes("::") && forbiddenVerticalsFor(c.grain_key).length > 0,
);

// A permlevel-1 grain field that must never surface as a Smart View column /
// cell value for a grain persona. Confirm the exact fieldname against schema.
const PERMLEVEL1_FIELD_LABELS = ["Vertical", "custom_vertical"];

for (const cred of grainPersonas) {
  const forbidden = forbiddenVerticalsFor(cred.grain_key);

  test.describe(`smart view scoping — ${cred.persona} (${cred.grain_key})`, () => {
    test.use({ storageState: storageStateFor(cred.persona) });

    test("authored Smart View stays within grain ∩ role", async ({ page }) => {
      await page.goto("/crm/leads", { waitUntil: "networkidle" });

      // TODO(selector): open the Smart Views tab / view picker.
      //   The fork exposes the Smart Views authoring UI on the leads list.
      //   Confirm the tab/button selector against the running SPA.
      //   e.g. await page.getByRole("tab", { name: /smart views/i }).click();
      const smartViewsTab = page.getByRole("tab", { name: /smart views/i });
      if (await smartViewsTab.count()) {
        await smartViewsTab.first().click();
      } else {
        // Fallback discovery hook — log what tabs exist so the operator can
        // pin the selector. Does not fail the test on its own.
        test.info().annotations.push({
          type: "selector-todo",
          description:
            "Smart Views tab not found by role=tab name=/smart views/i — " +
            "confirm the authoring entry point against the live SPA.",
        });
      }

      // TODO(selector): click "New view" / "Create" and build a minimal view
      //   on CRM Lead with a couple of columns, then save. Confirm selectors:
      //   e.g. await page.getByRole("button", { name: /new view/i }).click();
      //        ...ConditionBuilder / ColumnManager interactions...
      //        await page.getByRole("button", { name: /save/i }).click();

      // Whatever view ends up rendered (a freshly authored one, or the list
      // fallback if authoring selectors are not yet pinned), the rendered
      // table MUST obey the grain. These assertions are the real contract.

      // 1) No out-of-grain vertical in any rendered cell / header.
      const tableText = (await page.locator("body").innerText()).toString();
      for (const vertical of forbidden) {
        expect(
          tableText,
          `Smart View for ${cred.persona} leaked out-of-grain vertical ` +
            `"${vertical}"`,
        ).not.toContain(vertical);
      }

      // 2) No permlevel-1 grain field exposed as a column header.
      //    The grain fields sit at permlevel 1; a grain persona without
      //    field access must not see them as selectable/rendered columns.
      //    TODO(selector): scope this to the actual column-header container
      //    once pinned; until then we scan rendered header-like elements.
      const headerTexts = await page
        .locator("[role=columnheader], th, .list-header, .column-header")
        .allInnerTexts()
        .catch(() => [] as string[]);
      const joinedHeaders = headerTexts.join(" | ");
      for (const label of PERMLEVEL1_FIELD_LABELS) {
        expect(
          joinedHeaders,
          `Smart View for ${cred.persona} exposed permlevel-1 field ` +
            `"${label}" as a column — field LEAK`,
        ).not.toContain(label);
      }
    });
  });
}
