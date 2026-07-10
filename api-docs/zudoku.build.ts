import type { ZudokuBuildConfig } from "zudoku";

// The try-it playground calls the API on the SAME origin the docs are served from, so no
// CORS is ever involved: on each environment the docs live at `/docs` on the same host as
// `/api`. A single server (the deploy origin) is used so a cross-origin server can never be
// picked by accident. deploy-docs.sh sets DOCS_BASE_URL per environment (the site's public
// URL: http://localhost:8080 | https://one-uat.tatvacare.in | https://one.tatvacare.in).
//
//   `npm run build` (deploy) -> server = DOCS_BASE_URL (falls back to production)
//   `npm run dev`   (DOCS_ENV=local) -> server = the local bench at :8080
//
// Note: `npm run dev` serves the docs on :3000 while the API is on :8080, so the dev preview
// of try-it is cross-origin and needs Frappe CORS for the dev origin. To exercise try-it
// same-origin, validate on the DEPLOYED local docs at http://localhost:8080/docs instead.
const isLocal = process.env.DOCS_ENV === "local";
const base =
  process.env.DOCS_BASE_URL ||
  (isLocal ? "http://localhost:8080" : "https://one.tatvacare.in");

const buildConfig: ZudokuBuildConfig = {
  processors: [
    ({ schema }) => ({
      ...schema,
      servers: [{ url: base, description: "API base URL" }],
    }),
  ],
};

export default buildConfig;
