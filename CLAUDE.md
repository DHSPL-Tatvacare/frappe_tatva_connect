---
title: CLAUDE.md — tatva_connect repo constitution
tags: [frappe-crm, claude, repo-rules]
status: authoritative
updated: 2026-06-03
audience: any LLM or engineer working in this repository
---

# CLAUDE.md — working in this repo

This repo (`tatva_frappe_custom`, public: github.com/devops-tatvacare/tatva_frappe_custom) is the **code home** for the TatvaCare Frappe CRM: the custom app **`tatva_connect`** (WATI WhatsApp, Acefone telephony, future overrides) **and** the API docs site (`api-docs/`).

Three homes, kept separate — know which you're in:

| Home | Holds | Where |
|---|---|---|
| **This repo** | code + buildable artifacts (`tatva_connect/`, `api-docs/`, `docs/plans/`) | here |
| **Strategy vault** | research, designs, decisions, runbooks, live state | Obsidian: `tatvacare-obsidian/Projects/frappe-crm/Frappe Migration 101/` |
| **Auto-memory** | session-to-session operational facts | `~/.claude/projects/.../memory/` |

**For anything about the dev bench, the build/test loop, git flow, or shipping a change to prod → read the vault's `07-deploy-mechanics/` first.** This file does not repeat that; it states the invariants and points there.

---

## Repository layout (codified — do not reinvent)

A standard Frappe v15 app repo that also carries the docs site.

```
tatva_frappe_custom/                 # install with: bench get-app <repo-url>
├── tatva_connect/                   # the Frappe app PACKAGE (snake_case; scaffold via `bench new-app`)
│   ├── hooks.py                     # ★ ALL customization registers here (see Coding invariants)
│   ├── modules.txt  patches.txt     # module list / migrations (run on `bench migrate`)
│   ├── <module>/doctype/<dt>/       # custom doctypes
│   ├── api/                         # @frappe.whitelist() endpoints
│   ├── fixtures/                    # Custom Field / Property Setter JSON (auto-applied on migrate)
│   ├── wati/  acefone/              # the integrations (each has its own README.md)
│   └── public/{js,css}/  templates/ www/  tests/
├── api-docs/                        # Zudoku docs site → served at /docs (deploy-docs.sh)
├── docs/plans/                      # executable per-phase plans that ship with the code
├── nginx/frappe.conf.template       # frontend nginx template + the /docs route
└── pyproject.toml                   # ★ v15 packaging (NOT setup.py)  · README · license · .gitignore
```

Packaging rules: scaffold with `bench new-app` (never hand-build the skeleton); `pyproject.toml`, not `setup.py`; `.gitignore` excludes `node_modules/`, `dist/`, `__pycache__/`, and `.devbench/`.

---

## Coding invariants

1. **No fork. Ever.** Never edit or patch `frappe/crm`, `frappe_whatsapp`, or any upstream app, and never open PRs to them. All customization lives in `tatva_connect`, registered through **`hooks.py`**. The toolbox, in order of preference:
   - `override_doctype_class` — swap a controller (e.g. WATI's WhatsApp Message/Notification/Templates).
   - `override_whitelisted_methods` — swap a server method (e.g. Acefone's `make_a_call`, `get_call_log`).
   - `doc_events`, `scheduler_events` — hook validate/on_update/cron.
   - **Custom Field / Property Setter fixtures** — extend native doctypes as version-controlled JSON.
   - **CRM Form Script** (+ capture-phase DOM interception) — add/hijack frontend actions without touching the Vue bundle.
   - **Enable a native "slot", then override its backend** — e.g. Acefone rides the Exotel slot to get the native phone icon, with `make_a_call` overridden.
   - **If a clean override is genuinely impossible → STOP and surface it. Do not fork to force it.**
2. **Schema as code.** Custom fields, property setters, and patches live in `tatva_connect` and apply on `bench migrate`. No manual field creation on any environment.
3. **Single source of truth = this repo.** Edit here. The local bench runs a *copy* of the app — changes only take effect after you sync it in (see `07-deploy-mechanics/02-dev-workflow`).
4. **Clean, low-complexity functions.** Keep cyclomatic + cognitive complexity low; small functions; match the style, naming, and comment density of the surrounding code. No clever indirection, no speculative abstraction.
5. **Integration discipline.** WhatsApp is **WATI only — never Meta**, on every code path. Provider routing (WATI/Acefone accounts) is **most-specific-wins with no global default** — an unmatched lead is blocked, never sent through the wrong account. Every integration has a kill-switch. Secrets are env vars / Password fields — never committed (this repo is public).
6. **Never guess an API.** Read the source, Context7, or `--help` before using unfamiliar syntax. Don't fabricate config values or pretend access exists.
7. **No prefill / no seed unless structurally intrinsic.** A field `default`, or a seeded record, is allowed ONLY when the value is intrinsic to the feature's *structure* or its own *mechanism* — never when it's environment, connection, or business config an operator sets per deployment. The form gives the empty field; the operator fills it; sensible behaviour lives in a **code fallback**, not a baked form value.
   - **Prefill/seed OK (intrinsic — the "structural" bucket):** autoname / `naming_series` patterns (structure the doctype needs to function); a checkbox/number that starts `0`/unticked (the *absence* of a value, not a baked one); flags that define the feature's own logic identically in every deployment (e.g. a checklist item's `required`, a stage's `selectable` / `position` ordering).
   - **NO prefill/seed (operator fills, or code fallback):** connection/env values (container names, base URLs, endpoints, account names, keys, webhook secrets); business/config values (broadcast names, rate caps/limits, any name or threshold an operator picks); specific business artifacts/records (a particular Web Form, intake-form rows, demo doctors/hospitals) — created on the environment (prod DB), never shipped in code.
   - **Pattern:** prefer a code fallback (`value or DEFAULT`, `return True if val is None`) over a JSON form default for behaviour — the form stays blank, the code stays sensible. (Proven: rate cap, `base_url`, `is_enabled` all carried code fallbacks; their JSON defaults were redundant.)
   - **Litmus:** "Would two deployments legitimately set this differently?" → yes ⇒ no prefill. Extends invariant #2 (schema-as-code) and the seeding-tiers rule.

---

## Deploying invariants

**The full how-to is `07-deploy-mechanics/` in the vault.** The non-negotiables:

1. **Dev-first, always.** Build and prove on the local `.devbench`; deploy to prod only after local proof. Never experiment on prod.
2. **Two lanes, kept separate** — pick by what changed:
   - **App code** (`tatva_connect`) → bake into the prod image (`apps.json` → rebuild → `install-app`/`migrate`/`build`). Changes occasionally.
   - **Docs** (`api-docs/`) → fast file push (`./api-docs/deploy-docs.sh` → sites volume → nginx `/docs`). Changes often; **never** rides the image rebuild.
3. **Git:** feature branch → fast-forward merge into `main` → push. Pushing to GitHub does **not** touch prod. Commit only when a slice is locally proven (or when asked).
4. **No prod-downtime drama** unless it's live to customers (VPN-internal today).
5. **Only clean code + verified config promote — dev litter NEVER moves.** The dev bench is a scratchpad: it accumulates test users, throwaway leads, demo doctypes, ad-hoc patches. **None of that is a deploy artifact.** What moves to prod is exactly (a) the committed repo (code + doctypes + fixtures + patches) and (b) the explicit, documented config in the cutover plan (`docs/plans/`). If something works on dev but isn't in the repo or the plan, it does **not** exist for prod — re-create it cleanly from the plan, never copy the bench. This holds for this push and every future one.

---

## How we work (standing direction)

- **Plain English, tight responses.** Lead with the answer. Propose ONE thing, ask if it's good — no option matrices, no jargon walls, no closing summaries. The user is product-fluent, not DevOps — give exact commands for infra tasks.
- **Confirm before non-trivial edits.** For multi-step work: a 3–5 bullet plan, then wait. "Update and stop" means stop — no verification runs, no Playwright, no "let me also fix this."
- **Pause before anything risky** — editing a doctype field, running a migration, restarting the backend, touching compose, force-push, dropping a table. Explain in a few bullets and wait, even when the right move feels obvious.
- **LSQ + Frappe are first-class peers** during migration — both fire until Frappe reaches high fidelity, then LSQ writes are removed. Never call Frappe a fallback.

---

## Where to look

| Need | Go to |
|---|---|
| Dev bench, build/test loop, git, ship-to-prod, gotchas | vault `07-deploy-mechanics/` |
| Project strategy, data model, live state, runbooks | vault `Frappe Migration 101/CLAUDE.md` (primer) |
| WATI integration (authoritative) | vault `05-integrations/01-wati-whatsapp` · code `tatva_connect/wati/README.md` |
| Acefone telephony (authoritative) | vault `05-integrations/02-acefone-telephony` · code `tatva_connect/acefone/README.md` |
| Fresh-VM prod rebuild | vault `01-infrastructure/06-redeploy-end-to-end` |
