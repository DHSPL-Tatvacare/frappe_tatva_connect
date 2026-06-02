import type { ZudokuConfig } from "zudoku";

const config: ZudokuConfig = {
  site: {
    title: "TatvaCare CRM API",
    showPoweredBy: false,
  },
  basePath: "/docs",
  navigation: [
    {
      type: "category",
      label: "Documentation",
      icon: "book",
      items: [
        { type: "category", label: "Start here", items: ["introduction", "welcome", "quickstart"] },
        { type: "category", label: "Architecture", items: ["concepts"] },
        { type: "category", label: "Reference", items: ["errors", "operations"] },
      ],
    },
    { type: "link", to: "/api", label: "API Reference", icon: "code" },
  ],
  redirects: [{ from: "/", to: "/introduction" }],
  apis: {
    type: "file",
    input: "./openapi.json",
    path: "/api",
  },
  docs: {
    files: "/pages/**/*.{md,mdx}",
  },
};

export default config;
