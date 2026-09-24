import type { ZudokuBuildConfig } from "zudoku";

// A local build calls only the local bench; a deployed build keeps the spec's own servers.
const localUrl = process.env.DOCS_ENV === "local" ? process.env.ZUDOKU_PUBLIC_SITE_URL : undefined;

const buildConfig: ZudokuBuildConfig = localUrl
  ? { processors: [({ schema }) => ({ ...schema, servers: [{ url: localUrl, description: "Local" }] })] }
  : {};

export default buildConfig;
