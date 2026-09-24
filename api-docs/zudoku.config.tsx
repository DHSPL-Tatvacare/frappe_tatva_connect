import type { ZudokuConfig } from "zudoku";
import "./styles.css";
import { createApiIdentityPlugin } from "zudoku/plugins";
import { ApiKeyInput } from "./src/components/ApiKeyInput";
import { NotFound } from "./src/components/NotFound";
import { PostmanDownload } from "./src/components/PostmanDownload";
import { OPENAPI_PATH } from "./src/openapi";
import { ACTIVE_KEY, asAuthorization } from "./src/partner-key";

const basePath = "/docs";
const siteUrl = process.env.ZUDOKU_PUBLIC_SITE_URL;

const config: ZudokuConfig = {
  site: {
    title: "TatvaCare Partner API",
    logo: {
      src: { light: "/tatva-wordmark-icon-light.svg", dark: "/tatva-wordmark-icon-dark.svg" },
      alt: "TatvaCare Partner API",
      width: "180px",
    },
    showPoweredBy: false,
    sidebar: { togglePosition: "top", toggleVisibility: "hover" },
    notFoundPage: <NotFound />,
  },
  basePath,
  canonicalUrlOrigin: siteUrl,
  sitemap: siteUrl ? { siteUrl } : undefined,
  metadata: {
    favicon: "/tatva-new-icon.png",
    title: "%s",
    defaultTitle: "TatvaCare Partner API",
  },
  // Colours and styles live in styles.css; fonts stay here because Zudoku loads them.
  theme: {
    fonts: {
      sans: "Inter",
      mono: "JetBrains Mono",
    },
  },
  syntaxHighlighting: {
    themes: { light: "vitesse-light", dark: "vitesse-dark" },
    languages: ["shellscript", "json", "http", "python", "typescript"],
  },
  navigation: [
    {
      type: "category",
      label: "Documentation",
      icon: "book-open",
      items: [
        { type: "filter", placeholder: "Filter pages" },
        {
          type: "category",
          label: "Get started",
          collapsed: false,
          items: [
            { type: "doc", file: "partner-welcome", label: "Overview" },
            { type: "doc", file: "partner-validate-key", label: "Authentication" },
            { type: "doc", file: "partner-quickstart", label: "Quickstart" },
          ],
        },
        {
          type: "category",
          label: "Core concepts",
          collapsed: false,
          items: [
            { type: "doc", file: "concepts", label: "Lead identity" },
            { type: "doc", file: "discover-schema", label: "Schema discovery" },
            { type: "doc", file: "keywords", label: "Keywords" },
          ],
        },
        {
          type: "category",
          label: "Resources",
          collapsed: false,
          items: [
            { type: "doc", file: "leads", label: "Leads" },
            { type: "doc", file: "activities", label: "Activities" },
            { type: "doc", file: "calls", label: "Calls" },
            { type: "doc", file: "notes", label: "Notes" },
            { type: "doc", file: "files", label: "Files" },
            { type: "doc", file: "deals", label: "Deals" },
            { type: "doc", file: "tickets", label: "Tickets" },
            { type: "doc", file: "ticket-comments", label: "Ticket comments" },
          ],
        },
        {
          type: "category",
          label: "Working with the API",
          collapsed: false,
          items: [
            { type: "doc", file: "integration-patterns", label: "Integration patterns" },
            { type: "doc", file: "writes-and-retries", label: "Writes and retries" },
            { type: "doc", file: "responses", label: "Response shapes" },
            { type: "doc", file: "async-bulk", label: "Async bulk jobs" },
            { type: "doc", file: "partner-errors", label: "Errors" },
            { type: "doc", file: "rate-limits", label: "Rate limits" },
            { type: "doc", file: "conventions", label: "Conventions" },
          ],
        },
        {
          type: "category",
          label: "Updates",
          collapsed: false,
          items: [
            { type: "doc", file: "changelog", label: "Changelog" },
          ],
        },
      ],
    },
    { type: "link", to: "/partner-api", label: "API Reference", icon: "code" },
  ],
  navigationRules: [
    {
      type: "insert",
      match: "API Reference/0",
      position: "before",
      items: [{ type: "filter", placeholder: "Filter operations" }],
    },
  ],
  // Zudoku's ranking defaults, with a lower pageLength so long reference pages aren't pushed down.
  search: {
    type: "pagefind",
    maxSubResults: 5,
    transformResults: ({ result }) => !/\/(400|404|500)$/.test(result.url),
    ranking: { termFrequency: 0.8, pageLength: 0.3, termSimilarity: 1.2, termSaturation: 1.2 },
  },
  redirects: [
    { from: "/", to: "/partner-welcome" },
    { from: "/reading-values", to: "/responses" },
  ],
  apis: [
    {
      type: "file",
      input: "./openapi-partner.json",
      path: "/partner-api",
      publish: { path: OPENAPI_PATH, agentQuality: true },
      options: {
        supportedLanguages: [
          { value: "shell", label: "cURL" },
          { value: "python", label: "Python" },
        ],
      },
    },
  ],
  docs: {
    files: "/pages/**/*.{md,mdx}",
    llms: {
      llmsTxt: true,
      llmsTxtFull: true,
      description: "Send leads to the TatvaCare CRM and read them back, with their activities, calls, notes, files, deals, and support tickets.",
      instructions: [
        "Use these docs to build or debug a TatvaCare Partner API integration. Start with the Quickstart, then read Lead identity and Schema discovery, then the page for each resource you use. Test on UAT before production, and pace calls within the limits on the Rate limits page.",
        `Every endpoint, parameter, schema, and example is in the [OpenAPI spec](${basePath}${OPENAPI_PATH}).`,
      ].join("\n\n"),
    },
  },
  mdx: {
    components: { ApiKeyInput },
  },
  slots: {
    "head-navigation-end": PostmanDownload,
  },
  // An identity, not the native Authorize dialog: the playground applies one or the other, and only an identity can also set Content-Type.
  plugins: [
    createApiIdentityPlugin({
      getIdentities: async () => [
        {
          id: "partner-key",
          label: "Partner API key",
          authorizationFields: { headers: ["Authorization"] },
          authorizeRequest: (request: Request) => {
            try {
              const key = localStorage.getItem(ACTIVE_KEY);
              if (key) request.headers.set("Authorization", asAuthorization(key));
            } catch {
              // No storage during server rendering.
            }
            // Zudoku sends a typed body as text/plain; Frappe reads a body only when it is JSON.
            if (request.body !== null) {
              request.headers.set("Content-Type", "application/json");
            }
            return request;
          },
        },
      ],
    }),
  ],
};

export default config;
