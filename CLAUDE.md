---
title: CLAUDE.md — Frappe Migration 101 (primer)
tags: [frappe-crm, claude, context-primer]
status: current
updated: 2026-06-01
audience: any LLM (or new team member) starting work on this project
---

# CLAUDE.md — Frappe Migration 101

The primer. If you (LLM or human) just walked into this project, read this in full before doing anything else. Every claim below is sourced from a doc in this folder — follow the wikilinks for detail.

## What this folder is

The single source of truth for the TatvaCare Frappe CRM migration. Replaces LeadSquared. One Frappe install on one Azure VM. One folder for everything: infra, app config, data model, migration, integrations, runbooks, scripts.

This used to be scattered across `Architecture/`, `Decisions/`, `Developer/`, `Investigations/`, `Memory/`. All consolidated into here 2026-06-01. The old sub-folders no longer exist in the vault.

Folder convention:
- `NN-kebab-case.md` files (`NN` = 2-digit order within folder)
- Every doc has YAML frontmatter (`title`, `status`, `updated`)
- Wikilinks `[[file-name]]` use the **filename slug** (not path) — Obsidian resolves
- `99-archive/` holds superseded docs (never delete; one-line "superseded by" note when known)
- `scripts/` holds the executable scripts (vmssh, schema bootstrap, migration scripts)

## The 60-second context

| Question | Answer |
|---|---|
| What does this Frappe install hold? | Patient Support Program **leads** for TatvaCare. Replacing LeadSquared. |
| Where? | One Azure VM (`goodflip-care-frappe`, 4 vCPU / 16 GB RAM / 60 GB disk). |
| URL? | `https://one.tatvacare.in` (HTTPS terminated by an external Azure ingress, not the VM). |
| Stack? | Docker compose (11 services declared / 10 running steady-state) — `tatva-frappe:1.1` custom image with `frappe + crm + telephony + helpdesk + payments + lms`. |
| DB? | MariaDB 11.8 inside container `crm-db-1`. Site = `crm.local`. |
| Auth? | `Authorization: token <key>:<secret>` — but **API keys run the user's role/perm checks**, they do NOT bypass perms. |
| Data model? | **Lean core (~26 fields on CRM Lead) + 3 child "profile" doctypes** (Oncology / MyTatva Health / Metabolic). New verticals = new profile doctype, not new column. |
| Dedup key? | `mobile_no` (verified — 0 collisions on 11,762 prior leads). Format convergence to E.164 pending. |
| Status types? | New(Open) · Contacted/Nurture(Ongoing) · Qualified/Converted(Won) · **Junk/Unqualified(Lost — both require `lost_reason`)**. |
| Routing chain? | `Vertical (Product Line) → Group → Program`. All Links to lookup doctypes, all permlevel-1 (manager-only). |
| Custom endpoint? | One: `POST /api/method/upsert_lead_by_phone` — finds-or-creates a lead by `mobile_no`. Has a known silent-200 bug pending fix. |

## What lives where (this folder)

```
Frappe Migration 101/
├── CLAUDE.md                           ← you are here
├── 00-keep-in-mind.md                  ⭐ gotchas that have cost us hours — READ FIRST
├── README.md                           ← human-facing index
├── 01-infrastructure/                  ← VM, Docker, bench, nginx, redeploy runbook
│   ├── 01-vm-and-access.md             (creds, SSH, IPs)
│   ├── 02-docker-architecture.md       (11 services / 10 steady-state, image lineage, volumes)
│   ├── 03-bench-and-site-config.md     (common_site_config, site_config, server_script_enabled)
│   ├── 04-backup-runbook.md            (Ofelia backup, restore steps)
│   ├── 05-nginx-and-network-flow.md    (full traffic flow, frontend nginx config)
│   ├── 06-redeploy-end-to-end.md       ⭐ LINEAR step-by-step rebuild on a fresh VM
│   └── 07-observability-and-telemetry.md (metrics/logs/tracing — Sentry, OTel, Loki, Prometheus; what's built-in vs external)
├── 02-apps/                            ← CRM/Helpdesk/LMS app config
│   ├── 01-apps-installed.md            (5 apps, versions, role mapping)
│   ├── 02-app-launcher-and-routing.md  (/apps tile router, URL paths)
│   ├── 03-adding-frappe-apps-runbook.md
│   └── 04-lms-helpdesk-research.md     (peer-app reasoning + doctypes)
├── 03-crm/                             ← the migration target
│   ├── 01-data-model.md                ⭐ THE current-truth data model
│   ├── 02-doctypes-inventory.md        (live snapshot — 6 custom doctypes)
│   ├── 03-custom-fields-inventory.md   (live — 28 CRM Lead fields, 49+36+9 profile fields)
│   ├── 04-server-scripts-and-apis.md   (upsert API + REST shape)
│   ├── 05-roles-and-permissions.md     (4-tier role model)
│   ├── 06-decisions-log-data-model.md
│   ├── 07-decisions-log-permissions.md
│   ├── 08-frappe-code-reads.md         ⭐ source-verified findings from frappe/crm GitHub
│   ├── 09-learnings-and-gotchas.md
│   ├── 10-admin-setup-contract.md
│   ├── 11-admin-db-model-contract.md
│   └── 12-ui-layouts-state.md          (current Side Panel + Data Fields state — has known gaps)
├── 04-migration/
│   ├── 01-migration-overview.md        ⭐ where each workstream stands
│   ├── 02-lsq-anaya-migration.md       (M1+M2 done, M3+M4 pending)
│   ├── 03-parallel-write-implementation.md  (MyTatvaCore code spec)
│   ├── 04-steady-state-update-scope.md (phone-key, unique lock, vertical moves)
│   ├── 05-profile-architecture-plan.md (the rebuild plan we executed)
│   └── 06-scripts-inventory.md         (vmssh, schema bootstrap, migration scripts)
├── 05-integrations/
│   ├── 00-integrations-overview.md
│   ├── 01-wati-whatsapp.md             (WATI fork-and-port plan)
│   ├── 02-ozonetel-telephony.md        (Ozonetel adapter design)
│   ├── 03-mytatva-core-integration-contract.md  (what MyTatvaCore implements)
│   └── 04-lsq-export-and-orchestration.md
├── 06-runbooks/                        (operational how-tos, grows over time)
├── 99-archive/                         (superseded docs, memory snapshot, legacy indexes)
└── scripts/
    ├── vmssh.sh                        (expect-based SSH wrapper)
    ├── frappe-crm-create-fields-v2.py  ⚠️ LEGACY (pre-pivot v2 model — see scripts inventory)
    ├── frappe-crm-create-fields.py     ⚠️ LEGACY v1 — do NOT run
    ├── anaya-migrate-to-frappe.py      (LSQ → Frappe Anaya backfill)
    └── frappe-doctype-dump.py          (snapshot the schema)
```

## How to use this folder when working a task

| Task | Start at |
|---|---|
| "How do I rebuild this on a new VM?" | [[01-infrastructure/06-redeploy-end-to-end]] |
| "What does the data model look like?" | [[03-crm/01-data-model]] then [[03-crm/03-custom-fields-inventory]] |
| "How do I write a lead from an external system?" | [[03-crm/04-server-scripts-and-apis]] |
| "Why does the right pane show 19 tabs?" | [[03-crm/12-ui-layouts-state]] |
| "Why doesn't `read_only` work on a child table grid?" | [[03-crm/08-frappe-code-reads]] — Grid.vue has no read_only prop |
| "What stage is the migration at?" | [[04-migration/01-migration-overview]] |
| "I need to SSH the VM" | [[01-infrastructure/01-vm-and-access]] then `scripts/vmssh.sh` |
| "How do I add a Frappe app?" | [[02-apps/03-adding-frappe-apps-runbook]] |
| "WATI / Ozonetel design?" | [[05-integrations/01-wati-whatsapp]] · [[05-integrations/02-ozonetel-telephony]] |

## Standing user direction (DO NOT depart from these)

1. **LSQ + Frappe are first-class peers**, not sidecar. Both fire on every event until Frappe achieves "high fidelity", THEN we remove LSQ writes. Never call Frappe a fallback/secondary.
2. **WATI is non-negotiable** — chatbot flows + templates already on WATI. Never suggest Meta Cloud direct as a swap; always fork-and-port.
3. **No prod-snapshot/downtime warnings** unless the user says it's live to customers. The install is private to the team via VPN today.
4. **Plain English. Tight responses.** No jargon walls. No option matrices — propose ONE thing, ask if it's good. User is product-fluent, not DevOps; give exact commands for infra tasks. No closing summaries.
5. **No upstream contributions.** This is a private fork. Don't open PRs to `frappe/frappe`, `frappe/crm`, etc.
6. **Confirm before non-trivial edits.** For multi-step work: 3-5 bullet plan, then wait. "Update and stop" means stop — no verification, no Playwright, no "let me also fix this."
7. **Never guess library APIs.** Read source / Context7 / `--help`. Don't fabricate config values or pretend access exists.
8. **Phone format converges to E.164** (`+919999000123` — no hyphens, no spaces) once we own all writers. Migrated data is currently `+91-XXXXXXXXXX` — needs normalize-then-lock.
9. **Side Panel default-open rule: only the FIRST tab `opened: true`, all others `opened: false`.** Apply to every doctype's Side Panel (CRM Lead, CRM Deal, Contact, Organization, etc.) — agent never sees a wall of expanded sections. Whoever lays out the side panel orders tabs so the most-used tab is first.

## Live state to remember (don't re-verify each turn)

- 23 leads in the system today (20 sample + 3 webhook-test). The 12,060-lead Anaya backfill was wiped during the model rebuild; re-migration is pending.
- 1 server script (`Upsert Lead By Phone`) — has a known silent-200 bug; fix paused.
- 6 custom doctypes; 28 CRM Lead custom fields.
- Side Panel has 19 ghost tabs; Data Fields has 1 empty tab. Both fixes paused per user.
- `server_script_enabled=1` is set at the COMMON config level (NOT site). If you ever rebuild, set it via `bench set-config -g`, not `bench set-config`.
- `frappe.crm` Grid.vue has NO `read_only` prop — governance needs permlevel-1 + a validate hook in `tatva_crm` (custom app, not yet built).
- `disable_document_sharing=1` does NOT stop the `lead_owner` auto-DocShare (owner-share leak).

## What's NOT in this folder (and that's intentional)

- **Code** — lives in `/Users/dhspl/dummy/scripts/` (vault has a copy under `scripts/`) and in the MyTatvaCore repo.
- **Backups** — DB dumps live on the VM, not in the vault.
- **Hourly memory** — operational state lives in the auto-memory system at `~/.claude/projects/-Users-dhspl-dummy/memory/`. A snapshot of the memory as-of-2026-06-01 is in `99-archive/memory-snapshot-2026-06-01/`.

## When you're about to do something risky

Examples: change a doctype's field, run a migration script, restart the backend, edit a Server Script, touch the Docker compose file, force-push, drop a table.

→ Pause. Explain in 3-5 bullets. Wait for user confirmation. Even if "the right thing to do" feels obvious. The user has been burned by autonomous edits.

See also: [[README]] (human-facing index of this folder).
