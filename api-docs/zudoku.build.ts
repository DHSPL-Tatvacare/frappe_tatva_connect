import type { ZudokuBuildConfig } from "zudoku";

// The try-it playground offers every published environment, with the one this site is served
// from FIRST. Zudoku renders its server picker only when the spec carries more than one server
// (Endpoint.tsx) and defaults to servers[0], so the default is always the same-origin host: on
// each environment the docs live at `/docs` on the same host as `/api`, and that call needs no
// CORS. deploy-docs.sh sets DOCS_BASE_URL per environment (the site's public URL).
//
//   `npm run build` (deploy) -> this site's origin first, then the other environments
//   `npm run dev`   (DOCS_ENV=local) -> the local bench alone; a local page cannot reach either
//                                       deployed host, so offering them would only mislead
//
// Selecting a DIFFERENT environment makes the call cross-origin. That is by design — it is how a
// partner tries UAT from the production docs — and it works only where the operator has added the
// docs origin to `allow_cors` in that site's site_config.json. Without it the browser blocks the
// request; the key is never at risk either way, because a cross-origin fetch sends no cookie.
//
// Note: `npm run dev` serves the docs on :3000 while the API is on :8080, so the dev preview of
// try-it is cross-origin too. To exercise try-it same-origin, validate on the DEPLOYED local docs
// at http://localhost:8080/docs instead.
const PRODUCTION = "https://one.tatvacare.in";

// Every environment the Partner API is published on. Mirrors the table on the Conventions page.
const ENVIRONMENTS = [
  { url: PRODUCTION, description: "Production" },
  { url: "https://one-uat.tatvacare.in", description: "UAT (sandbox for testing)" },
];

const isLocal = process.env.DOCS_ENV === "local";
const base = process.env.DOCS_BASE_URL || (isLocal ? "http://localhost:8080" : PRODUCTION);

// The origin serving this build leads, so the picker's default is the one that always works.
const here = ENVIRONMENTS.find((s) => s.url === base) ?? { url: base, description: "This site" };
const servers = isLocal ? [here] : [here, ...ENVIRONMENTS.filter((s) => s.url !== here.url)];

const buildConfig: ZudokuBuildConfig = {
  processors: [({ schema }) => ({ ...schema, servers })],
};

export default buildConfig;
