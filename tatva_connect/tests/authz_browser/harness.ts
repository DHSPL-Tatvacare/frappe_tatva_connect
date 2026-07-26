/**
 * harness.ts — the creds/persona helpers, in a NON-test module.
 *
 * They used to live in `auth.setup.ts`. Playwright refuses to let one test file import another
 * ("test file X should not import test file auth.setup.ts"), and `auth.setup.ts` IS a test file
 * (the `setup` project runs it), so a spec importing them from there cannot start. They are the
 * same helpers, moved — `auth.setup.ts` re-exports them, so every existing import keeps working.
 * New specs import from HERE.
 */
import * as fs from "fs";
import * as path from "path";

const AUTH_DIR = path.join(__dirname, ".auth");

export type Persona = {
  persona: string;
  email: string;
  password: string;
  grain_key: string; // "vertical::group::program"
};

/** Resolve and parse the creds file. */
export function loadCreds(): Persona[] {
  const credsPath =
    process.env.AUTHZ_CREDS || path.join(__dirname, "authz_creds.json");
  if (!fs.existsSync(credsPath)) {
    throw new Error(
      `authz creds not found at "${credsPath}". Set AUTHZ_CREDS or place ` +
        `authz_creds.json next to the specs. The Python generator writes it ` +
        `into the bench site's private files; the runner copies it out.`,
    );
  }
  const raw = fs.readFileSync(credsPath, "utf-8");
  const creds = JSON.parse(raw) as Persona[];
  if (!Array.isArray(creds) || creds.length === 0) {
    throw new Error(`authz creds at "${credsPath}" is empty or not an array.`);
  }
  return creds;
}

/** storageState path for a persona. */
export function storageStateFor(persona: string): string {
  return path.join(AUTH_DIR, `${persona}.json`);
}

/** The vertical a grain persona must NEVER see leak (A2 same-program trap). */
export function forbiddenVerticalsFor(grainKey: string): string[] {
  const vertical = (grainKey || "").split("::")[0] || "";
  // The two grain personas that share program "Inside-Sales":
  //   grain_4 = Tatvapractice::India::Inside-Sales
  //   grain_5 = Goodflip::B2C::Inside-Sales
  // Each must show zero rows from the OTHER vertical's verticals.
  const ALL_VERTICALS = ["Tatvapractice", "Goodflip", "Goodflip-Care"];
  // "Goodflip" and "Goodflip-Care" are sibling verticals; a Tatvapractice
  // persona must see neither; a Goodflip persona must not see Tatvapractice.
  if (vertical === "Tatvapractice") return ["Goodflip", "Goodflip-Care"];
  if (vertical === "Goodflip" || vertical === "Goodflip-Care")
    return ["Tatvapractice"];
  // Unknown vertical: be conservative — forbid everything that isn't it.
  return ALL_VERTICALS.filter((v) => v !== vertical);
}

export const AUTH_STATE_DIR = AUTH_DIR;
