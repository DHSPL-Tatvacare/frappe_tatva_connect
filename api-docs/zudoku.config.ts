import type { ZudokuConfig } from "zudoku";
import { createApiIdentityPlugin } from "zudoku/plugins";
import { ApiKeyInput } from "./src/components/ApiKeyInput";
import { PostmanDownload } from "./src/components/PostmanDownload";

const config: ZudokuConfig = {
  site: {
    title: "TatvaCare CRM API Docs",
    logo: {
      // The wordmark = icon + "TatvaCare CRM API" text baked into ONE SVG.
      // Zudoku renders a single logo image (no separate title text), so the
      // combined wordmark is what carries both. (Favicon/browser-tab icon is
      // set separately in `metadata.favicon` to the tatva icon.)
      src: { light: "/tatva-wordmark-icon-light.svg", dark: "/tatva-wordmark-icon-dark.svg" },
      alt: "TatvaCare CRM API Docs",
      width: "186px",
    },
    showPoweredBy: false,
  },
  basePath: "/docs",
  metadata: {
    favicon: "/tatva-favicon.png",
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
        { type: "category", label: "Start here", items: ["introduction", "quickstart"] },
        { type: "category", label: "Frappe in plain English", items: ["frappe-doctypes", "frappe-child-tables", "frappe-forms", "frappe-hooks", "frappe-permissions", "frappe-rest-api", "frappe-discovery"] },
        { type: "category", label: "Architecture", items: ["concepts"] },
        { type: "category", label: "Channels", items: ["whatsapp", "telephony"] },
        { type: "category", label: "Administration", items: ["users", "roles-and-permissions"] },
        { type: "category", label: "Reference", items: ["errors", "errors-and-status", "jobs", "tags"] },
        {
          type: "category",
          label: "Partner API",
          items: [
            {
              type: "category",
              label: "Get started",
              items: [
                { type: "doc", file: "partner-welcome", label: "Overview" },
                { type: "doc", file: "partner-validate-key", label: "Authentication" },
                { type: "doc", file: "partner-quickstart", label: "Quickstart" },
              ],
            },
            {
              type: "category",
              label: "Working with leads",
              items: [
                { type: "doc", file: "discover-schema", label: "Schema discovery" },
                { type: "doc", file: "reading-values", label: "Reading responses" },
                { type: "doc", file: "responses", label: "Response shapes" },
              ],
            },
            {
              type: "category",
              label: "Reference",
              items: [
                { type: "doc", file: "partner-errors", label: "Errors" },
                { type: "doc", file: "rate-limits", label: "Rate limits" },
                { type: "doc", file: "conventions", label: "Conventions" },
              ],
            },
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
    { type: "modify", match: "Authentication", set: { collapsed: false } },
    // Partner API Reference: keep its single group expanded.
    { type: "modify", match: "Lead", set: { collapsed: false } },
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
  // Make <ApiKeyInput /> usable inside MDX (Validate-your-API-key page).
  mdx: {
    components: { ApiKeyInput },
  },
  // "Download for Postman" button (with the Postman logo) in the header, top-RIGHT
  // (head-navigation-end) — away from the TatvaCare logo on the left. (The site has no
  // footer rendered, so footer slots don't show; the header-end is the reliable spot.)
  slots: {
    "head-navigation-end": PostmanDownload,
  },
  // Persist the partner key across playground operations: paste it once
  // (ApiKeyInput saves to localStorage); this identity injects it on every request.
  plugins: [
    createApiIdentityPlugin({
      getIdentities: async () => [
        {
          id: "partner-key",
          label: "Partner API key",
          // Tell the playground this identity controls the Authorization header,
          // so selecting it wires + displays the injected value.
          authorizationFields: { headers: ["Authorization"] },
          authorizeRequest: (request: Request) => {
            try {
              const t = localStorage.getItem("tatva_partner_token");
              if (t) {
                request.headers.set(
                  "Authorization",
                  t.startsWith("token ") ? t : "token " + t,
                );
              }
            } catch {
              /* no storage (SSR) */
            }
            return request;
          },
        },
      ],
    }),
  ],
};

export default config;
