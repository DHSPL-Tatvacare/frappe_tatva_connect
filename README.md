# tatva-frappe-custom

Single working repo for TatvaCare's custom Frappe CRM work — custom app code (hooks/overrides) **and** the API documentation portal.

## Layout

| Path | What |
|---|---|
| `api-docs/` | The **Zudoku** docs site → served at `https://one.tatvacare.in/docs`. Buildable static project. |
| `api-docs/openapi.json` | Canonical API contract (drives the **API Reference** tab). |
| `api-docs/pages/*.mdx` | **Documentation** tab guide pages (hand-edited; grow these). |
| `api-docs/deploy-docs.sh` | Build + ship docs to the VM (content-only; no infra change). |
| `nginx/frappe.conf.template` | Frappe frontend nginx template = stock **+** the permanent `/docs` route. Bind-mounted via `crm-compose.yml`. |
| _(later)_ | custom Frappe app code — hooks, overrides, custom doctypes. |

## Docs

- **Update & deploy:** `cd api-docs && ./deploy-docs.sh` (needs node, npm, expect).
- **How `/docs` is served durably + full infra:** see the DR runbook **Phase 17** in the Obsidian vault (`Frappe Migration 101/01-infrastructure/06-redeploy-end-to-end.md`).

## Notes

- Strategy/architecture/runbook docs live in the Obsidian vault, not here. This repo holds **code + buildable artifacts**.
- The live VM/Docker is unchanged by editing this repo; only `deploy-docs.sh` (content) and runbook Phase 17 (one-time route setup) touch the server.
