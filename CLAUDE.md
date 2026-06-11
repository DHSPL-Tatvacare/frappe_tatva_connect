---
title: CLAUDE.md — tatva_connect repo constitution
tags: [frappe-crm, claude, repo-rules]
status: authoritative
updated: 2026-06-11
audience: any LLM or engineer working in this repository
---

# CLAUDE.md — working in this repo

This repo (`tatva_frappe_custom`, **public**: github.com/devops-tatvacare/tatva_frappe_custom) is the **code home** for the TatvaCare Frappe CRM: the custom app **`tatva_connect`** (WATI WhatsApp, Acefone telephony, intake forms, Azure storage, overrides) **and** the API docs site (`api-docs/`).

Two homes, kept separate — know which you're in:

| Home | Holds | Where |
|---|---|---|
| **This repo** | code + buildable artifacts + **these standing instructions** | here |
| **Strategy vault** | research, designs, decisions, runbooks, live project state, all facts | Obsidian: `tatvacare-obsidian/Projects/frappe-crm/` |

> Standing instructions = the **Invariants** below. They are non-negotiable. Auto-memory is not used for rules; project facts live in the vault.

---

## Repository layout (codified — do not reinvent)

A standard Frappe v15 app repo that also carries the docs site.

```
tatva_frappe_custom/                 # install with: bench get-app <repo-url>
├── tatva_connect/                   # the Frappe app PACKAGE (snake_case; scaffold via `bench new-app`)
│   ├── hooks.py                     # ★ ALL customization registers here
│   ├── modules.txt  patches.txt     # module list / migrations (run on `bench migrate`)
│   ├── seeds.py  schema_setup.py    # after_migrate: intrinsic seeds + un-fileable structural patches
│   ├── fixtures/                    # Custom Field / Property Setter JSON (schema-as-code, auto-applied)
│   ├── <module>/doctype/<dt>/       # custom doctypes (whatsapp/ telephony/ taxonomy/ intake/ storage/ ...)
│   ├── api/                         # @frappe.whitelist() endpoints
│   ├── taxonomy/                    # masters + shared master logic (normalize, program_mode, lookups)
│   └── public/{js,css}/  templates/ www/  tests/
├── db-seeds/                        # ★ gitignored — manual SQL the OPERATOR runs (business/master data)
├── archive/                         # ★ gitignored — dead/legacy code kept for bookkeeping only
├── api-docs/                        # Zudoku docs site → served at /docs (deploy-docs.sh)
├── docs/plans/                      # executable per-phase plans that ship with the code
└── pyproject.toml                   # ★ v15 packaging (NOT setup.py)  · README · license · .gitignore
```

Packaging: scaffold with `bench new-app`; `pyproject.toml`, not `setup.py`; `.gitignore` excludes `node_modules/`, `dist/`, `__pycache__/`, `.devbench/`, `db-seeds/`, `archive/`.

---

## INVARIANTS — non-negotiable

### A. Code & architecture

1. **No fork. Ever.** Never edit `frappe/crm`, `frappe_whatsapp`, or any upstream app, and never PR them. All customization lives in `tatva_connect`, registered via **`hooks.py`** — in order of preference: `override_doctype_class` → `override_whitelisted_methods` → `doc_events`/`scheduler_events` → Custom Field/Property Setter fixtures → CRM Form Script (+ capture-phase DOM) → enable a native slot and override its backend. **If a clean override is genuinely impossible, STOP and surface it — do not fork.**
2. **Single source of truth = this repo.** Edit here. The local bench runs a *copy*; changes only take effect after you sync them in.
3. **If it CAN be a file, it IS a file.** Doctype JSON, custom fields and property setters as fixtures — these build on every install. Use a patch / `after_migrate` **only** when it genuinely can't be a file: an **upstream doctype we can't fork**, or a value we must **merge-not-clobber** (e.g. appending to a stock Select whose options drift by version). Any structural change that isn't in a file MUST run on `after_migrate` — `install-app` **baselines `patches.txt` without running it**, so patch-only structures never land on a fresh DB.
4. **No prefill / no seed unless structurally intrinsic.** A field `default` or a seeded record is allowed ONLY when the value is intrinsic to the feature's *structure* or *mechanism*. Litmus: **"would two deployments legitimately set this differently?" → yes ⇒ no default.**
   - **OK:** `naming_series`/autoname; a checkbox/number that starts `0`/unticked (absence, not a baked value); a flag identical in every deployment (`required`, `selectable`, `position`).
   - **NOT OK:** connection/env values (URLs, endpoints, container/account names, keys, secrets); business/config values (broadcast names, rate caps, any operator-chosen name/threshold); specific business records (a Web Form, intake rows, demo doctors/hospitals).
   - **Pattern:** form stays blank; sensible behaviour lives in a **code fallback** (`value or DEFAULT`), never a baked form value.
5. **Business/master DATA is never auto-seeded.** It ships as **manual SQL in `db-seeds/`** (gitignored, named `YYYY-MM-DD-<commit>-*.sql`, idempotent `INSERT IGNORE`) that the **operator runs by hand**. Only reference data that is **identical in every deployment** may auto-seed via `after_migrate` (e.g. India cities).
6. **Code ships dormant.** Every integration/kill-switch defaults **OFF**; a blank/unsaved setting **reads as disabled**. Nothing fires until an operator enables it in the form.
7. **Composite `::` primary keys for grain-scoped masters — never `hash`.** A natural key (e.g. `vertical::group::program::name`) gives free uniqueness, idempotent hand-seeding, and legible runbooks. `title_field` shows the human name in the picker.
8. **One brain, not two.** Shared logic lives in one function/module both paths call (e.g. `program_mode.resolve_program` for partner API + intake). Never duplicate logic across paths.
9. **Lookups are server-scoped.** Filtered searches go through whitelisted query methods that enforce scope in the query, so masters stay **non-guest-readable** and the scope can't be widened by the client.
10. **Masters are first-class & curated.** Forms PICK from grain-scoped masters; a typed/"not listed" value is stored as text but **never auto-creates a master row**.
11. **Integration discipline.** WhatsApp is **WATI only — never Meta**, on every path. Provider routing (WATI/Acefone) is **most-specific-wins, no global default** — an unmatched lead is blocked, never sent through the wrong account. Every integration has a kill-switch. **Secrets are env vars / Password fields — never committed (this repo is public).**
12. **Clean, low-complexity code.** Small functions, low cyclomatic/cognitive complexity, correct abstractions, no speculative indirection. Match the surrounding style; **keep comments minimal**.
13. **Never guess an API or a fact.** Read the source, Context7, `--help`, or the live DB before using unfamiliar syntax. Never fabricate config, data, or access.
14. **Dead/legacy code is archived, not stranded.** Move it to gitignored `archive/`, remove it from the active code, and leave a commented trace in the relevant index file (e.g. `patches.txt`). Never silently delete.

### B. How to work with me

- **Do EXACTLY the narrow ask. Never extrapolate to adjacent changes.** A narrow instruction means *only* that — don't clean, refactor, or remove neighbouring things.
- **STOP AND ASK before anything destructive or out of scope** (deleting data, removing fields, touching prod, big refactors). **But once I say act, ACT** — don't keep re-asking.
- **Be confident. Don't flip-flop or back down.** Take the right engineering call and commit to it.
- **Don't stop short or defer.** Deliver the whole thing. If something "needs verifying," verify it yourself (read code, check the DB, research the API) — don't punt it back to me.
- **Don't burden me with decisions you can make.** If the answer is determinable, decide it.
- **Simple English, tight, lead with the answer.** No jargon walls, no option matrices, no closing-summary padding. Explain *why* in one line. Give exact commands for infra (I'm product-fluent, not DevOps).
- **"Update and stop" / "no code" / "just explain" / "investigate" mean literally that** — no verification runs, no screenshots, no "let me also fix this."

---

## Deploying invariants

1. **Dev-first, always.** Build and prove on the local `.devbench`; deploy to prod only after local proof. Never experiment on prod. On dev, keep WATI + Acefone + follow-up kill-switches OFF; use only the owner number for test leads.
2. **Two lanes, kept separate:** **app code** (`tatva_connect`) → bake into the prod image (`apps.json` → rebuild → `install-app`/`migrate`/`build`); **docs** (`api-docs/`) → fast file push (`deploy-docs.sh`), **never** rides the image rebuild.
3. **Git:** feature branch → fast-forward into `main` → push. Pushing to GitHub does **not** touch prod. Commit only when a slice is proven, or when asked.
4. **Only clean code + documented config promote — dev litter NEVER moves.** What goes to prod is exactly (a) the committed repo and (b) the explicit `db-seeds/` SQL + cutover plan. If it works on dev but isn't in the repo or the plan, it does not exist for prod — re-create it cleanly, never copy the bench.

---

## Where to look

| Need | Go to |
|---|---|
| Project primer, strategy, data model, live state | vault `Projects/frappe-crm/CLAUDE.md` |
| Naming / PK conventions | vault `01-reference/crm-model/11-naming-conventions.md` |
| Dev bench, build/test loop, ship-to-prod | vault `02-operations/deploy-mechanics/` · runbooks `02-operations/runbooks/` |
| WATI WhatsApp (authoritative) | vault `03-integrations/01-wati-whatsapp` · code `tatva_connect/whatsapp/` |
| Acefone telephony (authoritative) | vault `03-integrations/02-acefone-telephony` · code `tatva_connect/telephony/` |
| Fresh-VM prod rebuild | vault `02-operations/runbooks/07-redeploy-end-to-end.md` |
