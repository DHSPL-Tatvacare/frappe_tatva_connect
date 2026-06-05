import type { ZudokuConfig } from "zudoku";

const config: ZudokuConfig = {
  site: {
    title: "TatvaCare CRM API Docs",
    logo: {
      src: { light: "/tatva-wordmark-light.svg", dark: "/tatva-wordmark-dark.svg" },
      alt: "TatvaCare CRM API Docs",
      width: "180px",
    },
    showPoweredBy: false,
  },
  basePath: "/docs",
  metadata: {
    favicon: "/tatva_logo.jpeg",
    title: "%s",
    defaultTitle: "TatvaCare CRM API Docs",
  },
  // A lively, branded palette. Teal primary reads well in both light and dark.
  theme: {
    light: {
      primary: "174 72% 36%",
      primaryForeground: "0 0% 100%",
    },
    dark: {
      primary: "172 66% 50%",
      primaryForeground: "180 60% 8%",
    },
    // Keep reference tables compact and neat.
    customCss: {
      "table": {
        "font-size": "0.9rem",
      },
    },
  },
  navigation: [
    {
      type: "category",
      label: "Documentation",
      icon: "book",
      items: [
        { type: "category", label: "Start here", items: ["introduction", "welcome", "quickstart"] },
        { type: "category", label: "Frappe in plain English", items: ["frappe-doctypes", "frappe-child-tables", "frappe-rest-api", "frappe-discovery", "frappe-permissions"] },
        { type: "category", label: "Architecture", items: ["concepts"] },
        { type: "category", label: "Channels", items: ["whatsapp", "telephony"] },
        { type: "category", label: "Administration", items: ["users", "roles-and-permissions"] },
        { type: "category", label: "Reference", items: ["errors", "errors-and-status", "operations", "jobs", "tags"] },
        {
          type: "category",
          label: "Partner API",
          icon: "key-round",
          items: [
            { type: "doc", file: "partner-welcome", label: "Welcome" },
            { type: "doc", file: "partner-validate-key", label: "Validate your API key" },
            { type: "doc", file: "partner-quickstart", label: "Quickstart" },
            { type: "doc", file: "discover-schema", label: "Discover your schema" },
            { type: "doc", file: "reading-values", label: "Reading values" },
            { type: "doc", file: "responses", label: "Response shapes & codes" },
            { type: "doc", file: "partner-errors", label: "Errors" },
            { type: "doc", file: "rate-limits", label: "Rate limits" },
            { type: "doc", file: "conventions", label: "Conventions" },
          ],
        },
      ],
    },
    { type: "link", to: "/api", label: "API Reference", icon: "code" },
    { type: "link", to: "/partner-api", label: "Partner API Reference", icon: "key-round" },
  ],
  search: { type: "pagefind" },
  navigationRules: [
    // API Reference: keep "Getting started" expanded; expandAllTags:false collapses the rest.
    { type: "modify", match: "Getting started", set: { collapsed: false } },
    // Partner API Reference: keep its single group expanded.
    { type: "modify", match: "Partner Lead API", set: { collapsed: false } },
  ],
  redirects: [{ from: "/", to: "/introduction" }],
  apis: [
    {
      type: "file",
      input: "./openapi.json",
      path: "/api",
      options: {
        // Collapse every API-reference tag group by default; tags opt back open via
        // `x-zudoku-collapsed: false` in openapi.json.
        expandAllTags: false,
      },
    },
    {
      type: "file",
      input: "./openapi-partner.json",
      path: "/partner-api",
    },
  ],
  docs: {
    files: "/pages/**/*.{md,mdx}",
  },
};

export default config;
