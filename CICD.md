<!-- AUTHORITATIVE deploy posture. Keep current whenever the deploy process changes. Updated 2026-06-29. -->

# CICD.md — deploy posture (codified)

How `tatva_connect` + the CRM fork reach UAT/prod. This is the finalized posture: **greenfield Frappe
v16, split DB (app box and DB box on one private VNet), no data carried — real data arrives via the LSQ
migration after**. Step-by-step runbook: `docs/prod-deploy/DEPLOY.md`. Seed details: `docs/go-live/3-seed/db-seeds/INDEX.md`.

## The posture in one line
Build one image from the env's apps file → bring up boxes → `bench migrate` (applies ALL schema-as-code) →
`enable-scheduler` → operator runs `docs/go-live/3-seed/db-seeds` → publish handbook → enable config → LSQ migration → cutover.

## Branches & environments (codified)
Solo flow, **no PRs**: in the CRM fork, `develop` (default) → `uat` → `prod`.
- **local** = the dev bench tracks `develop` (`git checkout develop` — git clone, no image).
- **UAT / PROD** = image built from the matching apps file, which pins **both our repos** per env:
  `apps.json` (crm + connect `develop`) · `apps.uat.json` (both `uat`) · `apps.prod.json` (both `prod`).
  Everything else (frappe/whatsapp apps) is identical & pinned. Both repos follow the same
  `develop → uat → prod` flow; there is no `main`. Build picks the env: `APPS_JSON_BASE64=$(base64 -w0 apps.<env>.json)`.
- **Promote by fast-forward** when green (`git merge --ff-only`), then rebuild that env's image. Branch name = env.
- **CI (fork only), purely push-triggered (no cron):** `Frontend CI` (ESLint/Oxlint/Vitest) on every push to all
  three; `Backend CI` (heavy) only on pushes to `uat`/`prod` (i.e. promote/ff-merge). Blocking is enforced by branch
  protection on `prod`. No external services (Codecov/Semgrep stripped); SAST lives in `tatva_connect` (tcsec).

## Conventions — commits & workflows (codified)
**Commits — Conventional Commits** (both repos): `type(scope): summary` — lowercase, imperative, no period,
≤72-char summary; blank line, then the body (the *why*, not the what). One logical change per commit.
- **type:** `feat` `fix` `refactor` `perf` `test` `docs` `ci` `build` `chore` `revert`
- **scope:** the area, lowercase — `lead` `grain` `mobile` `modals` `tasks` `notes` `whatsapp` `telephony`
  `smartview` `access` `automation` `deploy` `ci` `deps` (omit only if truly global).
- e.g. `feat(lead): grain is entitlement-driven` · `ci: strip Codecov` · `fix(mobile): content-sized sheets`.

**Workflow names** (GitHub Actions, fork) — pattern `<Area> CI`, job names are plain nouns:
`Frontend CI` (jobs `Lint`, `Tests`) · `Backend CI` (job `Tests`).

## Two lanes — what is automated, what is manual, and why

| | Lane 1 — AUTOMATED (CI + `bench migrate`) | Lane 2 — MANUAL (operator) |
|---|---|---|
| Runs | image build/push, install apps, `migrate`, `enable-scheduler` | `docs/go-live/3-seed/db-seeds`, handbook publish, config enablement, LSQ migration |
| Carries | code, structure, schema (doctypes, fields, patches) — **no business values** | business/master data, secrets, on/off switches |
| Why manual | because these are **per-deployment choices** (which grains, which secrets, what to switch on) — baking them in would violate "code ships dormant / no hardcoding" | |
| Idempotent | yes (`migrate` re-runs cleanly) | yes (every seed is `INSERT IGNORE`/`ON DUPLICATE`) |

Both lanes must finish for "deploy = done." Lane 1 alone gives a working but **inert** site: tables exist,
30 automation toggles exist DORMANT, zero business data, nothing sends.

## Lane 1 — build + migrate (CI / DevOps)
1. **Build image** from the env's apps file (`apps.uat.json`/`apps.prod.json`; 9 apps: CRM fork@`uat`/`prod`
   + frappe_whatsapp, telephony, helpdesk, payments, lms, wiki, insights, tatva_connect) on Frappe `v16`
   (`FRAPPE_CORE_REF=v16.22.0`). Tag by
   `$GIT_SHA`, never `latest`. Push to ACR.
2. **Boxes up, create site, install apps** (`crm` + `frappe_whatsapp` before `tatva_connect` — its
   `required_apps`). Flush redis-cache before migrate (v16 module-map gotcha).
3. **`bench migrate`** applies everything schema-as-code: doctype JSON, fixtures (Custom Fields/Property
   Setters), `patches.txt`, then the **10-step `after_migrate` orchestration**:
   schema patches → DocPerm lockdown → intrinsic seeds (India cities, side-effects) → automation catalog
   seed (30 `CRM Tatva Automation` toggles, DORMANT) → **3 GATES that ABORT the migrate on drift**
   (automation drift · notification drift · lockdown-open) → form/client scripts → draft folder.
   A throw at a gate = a missing `automation/registry.py` row (code), **not a flaky deploy**: fix the
   named file, rebuild. You run nothing by hand here.
4. **`bench enable-scheduler`** — required, or the 4 cron jobs (template re-sync, observability rollup,
   run-log sweep, draft purge) never fire.

## Lane 2 — operator, post-migrate (in order)
1. **Seeds:** `cd docs/go-live/3-seed/db-seeds && ./apply-seeds.sh <site> <backend-container>` — runs `seeds.manifest` in
   dependency order (masters before rows). See `docs/go-live/3-seed/db-seeds/INDEX.md`.
2. **Handbook → Wiki:** run `publish.py` (greenfield Wiki starts empty). DEPLOY.md §2b.
3. **Enable config** (below).
4. **LSQ data migration:** real leads/activities (`docs/go-live/7-migrate-data/MIGRATION.md`), last.
5. **Cutover:** point the edge/LB at the app box, smoke-test, decommission old prod.

## Config enablement — two moves per feature
Everything ships DORMANT. To turn a feature on: **(a) fill its Settings form** (secrets from the vault,
never the repo) **then (b) flip its `CRM Tatva Automation` toggle**. A filled form with the toggle OFF
does nothing. The 30 toggles are auto-seeded dormant; the full feature→form→toggle map is in
`docs/go-live/3-seed/db-seeds/INDEX.md` (Part 3). Examples: WhatsApp → CRM WhatsApp Settings + `WhatsApp::WATI::messaging`;
Telephony → CRM Telephony Settings + `Telephony::Acefone::calls`; Azure files → CRM Azure Storage Settings
+ `Storage::Azure::offload`; enrolment form → CRM Intake Settings + `Lead::Enrolment::intake`.

## Deploy invariants (non-negotiable — mirror of CLAUDE.md A/S)
- **No config in code.** Connection/env values, secrets, business names, thresholds, specific records are
  NEVER baked in (no field default, no fixture, no seed). They are operator-entered. (CLAUDE.md A.4.)
- **No business data auto-seeds.** Master/business data is operator-run `docs/go-live/3-seed/db-seeds/` SQL only; just
  deployment-identical reference data (India cities) auto-seeds. (A.5.) **Never stuff data into `seeds.py`.**
- **Code ships dormant** — every integration/switch defaults OFF; a blank setting reads as disabled. (A.6.)
- **Smart Views and automation Rules are user-built — never seeded.** Fresh DB starts empty. (A.17.)
- **Secrets in env/Password fields only** — the repo is PUBLIC. (S.5.)
- **Dev-first.** Prove on the `.devbench` before prod; never experiment on prod.
- **Dev litter never promotes.** Only the committed repo + the explicit `docs/go-live/3-seed/db-seeds/` SQL + cutover plan go
  to prod. If it works on dev but isn't in the repo or the plan, re-create it cleanly — never copy the bench.
- **Docs ship on a separate lane** (`api-docs/` → `deploy-docs.sh`), never on the image rebuild.
- **Image by `$GIT_SHA`, never `latest`.** App and DB on the same private VNet; DB on its own SSD, no public IP.

## Validation, rollback, backups
- **Validate on a clean install** (recount; see DEPLOY.md): masters/catalog present; `tatva_automation 30`
  (all dormant); `smart_views 0`; `crm_automation_rule 0`; integrations OFF.
- **Rollback:** redeploy the prior `$GIT_SHA` image + restore the pre-deploy DB backup (image rollback
  without DB restore leaves `installed_apps` dangling).
- **Backups:** scheduled `bench backup --with-files` → private Azure Blob via Managed Identity (no stored
  secret), lifecycle retention + GRS. A backup must live on different hardware than the DB. DEPLOY.md §10.

## Index
`CLAUDE.md` / `AGENTS.md` (rules) · `docs/prod-deploy/DEPLOY.md` (runbook) · `docs/go-live/3-seed/db-seeds/INDEX.md` (seeds +
config map) · `docs/INVENTORY.md` (what the app adds) · `tatva_connect/patches.txt` (migration order).
