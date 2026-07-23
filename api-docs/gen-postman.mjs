// Generate the downloadable OpenAPI file (imported into Postman) from the canonical partner
// spec. Runs on `prebuild`, so the download can never drift from openapi-partner.json.
// Output is gitignored — it is a build artifact, not source.
//
// UAT is listed FIRST so an imported collection defaults to the sandbox, never production.
// The deployed try-it playground is same-origin-locked (zudoku.build.ts) and cannot reach
// prod by accident; a downloaded collection can, so its default must be the safe host.
import { readFileSync, writeFileSync } from "node:fs";

const src = new URL("./openapi-partner.json", import.meta.url);
const out = new URL("./public/tatvacare-partner-api.openapi.json", import.meta.url);

const spec = JSON.parse(readFileSync(src, "utf8"));
spec.servers = [
  { url: "https://one-uat.tatvacare.in", description: "UAT (sandbox for testing)" },
  { url: "https://one.tatvacare.in", description: "Production" },
];

writeFileSync(out, JSON.stringify(spec, null, 2) + "\n");
console.log("gen-postman: wrote public/tatvacare-partner-api.openapi.json");
