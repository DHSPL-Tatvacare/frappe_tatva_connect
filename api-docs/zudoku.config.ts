import type { ZudokuConfig } from "zudoku";
import { createApiIdentityPlugin } from "zudoku/plugins";
import { ApiKeyInput } from "./src/components/ApiKeyInput";
import { Mermaid } from "./src/components/Mermaid";
import { PostmanDownload } from "./src/components/PostmanDownload";

const config: ZudokuConfig = {
  site: {
    title: "TatvaCare Partner API",
    logo: {
      // The wordmark = icon + "TatvaCare Partner API" text baked into ONE SVG.
      // Zudoku renders a single logo image (no separate title text), so the
      // combined wordmark is what carries both. (Favicon/browser-tab icon is
      // set separately in `metadata.favicon` to the tatva icon.)
      src: { light: "/tatva-wordmark-icon-light.svg", dark: "/tatva-wordmark-icon-dark.svg" },
      alt: "TatvaCare Partner API",
      width: "216px",
    },
    showPoweredBy: false,
  },
  basePath: "/docs",
  metadata: {
    favicon: "/tatva-new-icon.png",
    title: "%s",
    defaultTitle: "TatvaCare Partner API",
  },
  // A lively, branded palette. Teal primary reads well in both light and dark.
  theme: {
    // Match the Frappe wiki: Inter as the UI/body font (the wiki uses
    // InterVariable). Zudoku loads Inter from its built-in font set.
    fonts: {
      sans: "Inter",
    },
    light: {
      primary: "174 72% 36%",
      primaryForeground: "0 0% 100%",
    },
    dark: {
      primary: "172 66% 50%",
      primaryForeground: "180 60% 8%",
    },
    // Shrink only the CONTENT text to the wiki's 14px feel — NOT the whole
    // layout. The doc body is Tailwind `.prose`, whose children size in em off
    // the prose base, so setting the base to 0.875rem (14px) scales the reading
    // text down while the page width, sidebar, and spacing stay at full size.
    customCss: {
      // Layout background. Zudoku paints the whole app (content, header, and the left nav)
      // with `bg-background`, so this one variable is the overall page color. Selectors carry
      // higher specificity than Zudoku's `:root` / `.dark` so they win regardless of load order.
      // Light #ffffff, dark #0f0f0f.
      "html:not(.dark)": { "--background": "#ffffff" },
      "html.dark": { "--background": "#0f0f0f" },
      // Left navigation sidebar. The docs theme gives it no separate color token (it inherits
      // `bg-background`), so its distinct colour is painted on the nav wrapper directly. That
      // wrapper is the only `data-pagefind-ignore` element that also carries `border-r`.
      // Light #f8f8f8 (distinct from the #ffffff content); dark #0f0f0f (flat with the layout).
      "html:not(.dark) [data-pagefind-ignore='all'].border-r": { "background-color": "#f8f8f8" },
      "html.dark [data-pagefind-ignore='all'].border-r": { "background-color": "#0f0f0f" },
      // Doc/markdown body text -> 14px (children scale via em).
      ".prose": {
        "font-size": "0.875rem",
      },
      // API-reference headings (operation titles + section labels) live OUTSIDE
      // .prose and use Tailwind text-* utilities. Scope the scale-down to `main`
      // so the content type tightens to the wiki feel without touching the nav,
      // sidebar, or logo.
      "main .text-2xl": {
        "font-size": "1.25rem",
      },
      "main .text-xl": {
        "font-size": "1.0625rem",
      },
      "main .text-lg": {
        "font-size": "0.95rem",
      },
      "table": {
        "font-size": "0.9rem",
      },
    },
  },
  // Exactly two top tabs: Documentation (all guide content, grouped in the left
  // sidebar) and API Reference (the partner OpenAPI spec). The internal REST reference
  // lives in the login-gated handbook, never on this public site (see docs/go-live/4-handbook).
  navigation: [
    {
      type: "category",
      label: "Documentation",
      icon: "book",
      items: [
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
          ],
        },
        {
          type: "category",
          label: "Working with the API",
          collapsed: false,
          items: [
            { type: "doc", file: "writes-and-retries", label: "Writes and retries" },
            { type: "doc", file: "responses", label: "Response shapes" },
            { type: "doc", file: "reading-values", label: "Reading values" },
            { type: "doc", file: "files", label: "Files" },
            { type: "doc", file: "async-bulk", label: "Async bulk jobs" },
            { type: "doc", file: "partner-errors", label: "Errors" },
            { type: "doc", file: "rate-limits", label: "Rate limits" },
            { type: "doc", file: "conventions", label: "Conventions" },
          ],
        },
      ],
    },
    { type: "link", to: "/partner-api", label: "API Reference", icon: "code" },
  ],
  search: { type: "pagefind" },
  redirects: [{ from: "/", to: "/partner-welcome" }],
  apis: [
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
    components: { ApiKeyInput, Mermaid },
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
