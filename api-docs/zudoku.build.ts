import type { ZudokuBuildConfig } from "zudoku";

// Environment-aware API servers for the playground.
//   `npm run dev`   -> DOCS_ENV=local -> localhost dev bench is the default, prod still selectable
//   `npm run build` -> (deploy)       -> production only
// This overrides the `servers` block in openapi.json at build/dev time, so the
// committed openapi.json stays prod-canonical and there's no git churn.
const PROD = { url: "https://one.tatvacare.in", description: "Production" };
const LOCAL = { url: "http://localhost:8000", description: "Local dev bench (dev.localhost)" };

const isLocal = process.env.DOCS_ENV === "local";

const buildConfig: ZudokuBuildConfig = {
  processors: [
    ({ schema }) => ({ ...schema, servers: isLocal ? [LOCAL, PROD] : [PROD] }),
  ],
};

export default buildConfig;
