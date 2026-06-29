<!-- AUTHORITATIVE agent constitution. AGENTS.md is a byte-identical mirror — edit BOTH. Updated 2026-06-29. -->

# Working in this repo (read before any change)

`frappe_tatva_connect` (PUBLIC) is the code home for TatvaCare's Frappe CRM: the custom app
**`tatva_connect`** + the API docs site (`api-docs/`). Strategy/facts live in the Obsidian vault
(`tatvacare-obsidian/Projects/frappe-crm/`), NOT here. This file is the rule set; obey it exactly.

## What Frappe is, and how we use it
Frappe is an open-source low-code framework: Python backend, Vue/JS frontend, an ORM where every
table is a **DocType**, a built-in **permission engine**, REST API, background jobs, and the Desk UI.
A Frappe "app" is a pluggable module set. Our bench (Frappe **v16**) installs these apps:
**CRM** (our lead/sales pipeline — FORKED), **Helpdesk** (tickets), **LMS** (training), **Insights** (BI),
**Wiki** (handbook), **Payments**, **Telephony**, **frappe_whatsapp** — all used near-stock — plus our
**`tatva_connect`** on top. We customize the platform through `tatva_connect`; we never fork core.

## Grain (the core concept — know it cold)
A **grain** = `vertical :: group :: program` (e.g. `GoodFlip Care::Anaya::Nivolumab`). It scopes
everything — masters, lead routing, task types, smart-view fields, assignment, row visibility,
permissions. Grain is **resolved dynamically** from the acting user's entitlement (one grain →
auto-applied; manager → validated pick) and stamped on the lead before `validate`. Grain-scoped
masters use composite `::` primary keys, never `hash`.

## What we built
- **`tatva_connect` (this repo, backend):** ALL customization — WhatsApp (WATI), telephony (Acefone),
  intake/enrolment forms, Azure Blob storage, partner Lead API, the automation engine, smart-view
  engine, observability, access/VAPT hardening. **59 custom doctypes**, registered in `hooks.py`.
  Modules: `whatsapp telephony taxonomy lead tasks activity automation notifications intake storage
  partner_api smartview observability access location`. Full list → `docs/INVENTORY.md`.
- **The CRM fork** (`DHSPL-Tatvacare/frappe_tatva_crm`, branches `develop`→`uat`→`prod`): thin, guarded
  extension points ONLY — no business logic. Reason: the Vue SPA exposes no client hook for list/task actions.
- **Key overrides (why):** WhatsApp Message/Notification/Templates → `whatsapp.*` (WATI, never Meta);
  `File` → Azure offload; `Assignment Rule` → grain-gated; exotel call → `telephony.bridge`; 11
  `access.native_guards` wrap engine-bypassing native crm methods (VAPT fail-closed).

## Repo layout (do not reinvent)
`tatva_connect/` = the app: `hooks.py` (★ all customization registers here), `modules.txt patches.txt`,
`schema_setup.py seeds.py` (after_migrate), `fixtures/` (schema-as-code), `<module>/doctype/<dt>/`,
`api/` (@frappe.whitelist), `public/{js,css} tests/`. `db-seeds/` ★gitignored (operator SQL + INDEX.md +
seeds.manifest). `docs/` (INVENTORY.md, prod-deploy/DEPLOY.md, plans/, migration/). `archive/` ★gitignored.
`api-docs/` (Zudoku → /docs). `pyproject.toml` (NOT setup.py).

## A. Architecture invariants (non-negotiable)
- **A.1** Thin fork of `frappe/crm` ONLY — never core/`frappe_whatsapp`/other apps. Customize in this
  order, fork LAST: `override_doctype_class` → `override_whitelisted_methods` → `doc_events`/
  `scheduler_events` → Custom Field/Property Setter fixtures → CRM Form Script → native slot. Every
  forked file: `// TATVA:` marker + `CUSTOMIZATIONS.md` entry; each hook guarded → 100% stock without us.
- **A.2** Code ONLY in this repo. The bench is a disposable run/verify copy — never edit/commit/reset a
  bench app folder without explicit per-action permission. Read-only inspection is fine.
- **A.3** If it CAN be a file, it IS a file (doctype JSON + fixtures, apply on migrate). Patch/
  `after_migrate` only when it genuinely can't (upstream doctype, or merge-not-clobber a stock Select).
- **A.4** No prefill/seed unless structurally intrinsic. Litmus: "would two deployments set this
  differently?" → yes ⇒ no default. Behaviour lives in a code fallback (`value or DEFAULT`).
- **A.5** Business/master DATA never auto-seeds — ships as operator-run `db-seeds/` SQL (idempotent).
  Only deployment-identical reference data (India cities) auto-seeds via `after_migrate`.
- **A.6** Code ships DORMANT — every integration/switch defaults OFF; a blank setting reads as disabled.
- **A.7** Composite `::` PKs for grain-scoped masters, never `hash`.
- **A.8** One brain, not two — shared logic in one function both paths call. Never duplicate.
- **A.9** Lookups are server-scoped (whitelisted query methods; masters non-guest-readable).
- **A.10** Masters first-class & curated — forms PICK from masters; a typed value stores as text, never
  auto-creates a master row.
- **A.11** WhatsApp = WATI only, never Meta. Provider routing most-specific-wins, no global default
  (unmatched lead BLOCKED). Every integration has a kill-switch.
- **A.12** Clean, low-complexity code; match surrounding style; keep comments minimal.
- **A.13** Never guess an API or fact — read source/Context7/`--help`/the live DB first. Never fabricate.
- **A.14** Dead code is ARCHIVED (gitignored `archive/`) + a commented trace, never silently deleted.
- **A.15** File privacy fail-closed — every attachment private unless its doctype is `*Settings` or
  operator-listed public. Never hardcode a privacy list.
- **A.16** No best-guess inbound attribution — match phone + receiving account's grouping (token/DID);
  no hit ⇒ drop + log, never fall back to any-lead-by-phone.
- **A.17** Smart Views are 100% user-built — NEVER seeded (no `is_standard`/patch/fixture/db-seed view).
  Ship the engine; fresh DB = empty Smart Views tab. (Same applies to automation RULES.)
- **A.18** `import frappe` FIRST — use native helpers, never hand-roll: `db.get_value/get_all/set_value/
  exists/count`, `db.rename_column/add_index/has_column`, `make_get/post_request`, `parse_json/as_json`,
  `now_datetime/getdate/cint/flt/cstr`, `frappe.conf` + `get_password`, `throw/log_error/enqueue/cache/
  has_permission/generate_hash`. Hand-roll ONLY when no native equivalent (or pre-model-sync DDL),
  **with explicit user sign-off**, tagged `# sqli-ok:`/`# authz-ok:`.

## S. Security invariants (mandatory, fail-closed)
- **S.1** Enforce the Frappe permission layer on EVERY path — `frappe.has_permission`/`only_for`, never a
  hand-rolled role-string gate. Server-side backstops survive even if a frontend hook is gone.
- **S.2** No SQL attack surface — never string-interpolate a value into SQL. Bind every value `%(name)s`,
  escape identifiers via `frappe.db.escape`. Unmarked interpolation FAILS CI (`tests/test_no_sql_injection.py`).
- **S.3** VAPT gates stay — `access.native_guards` wrap engine-bypassing native methods; `access.lockdown`
  rebuilds the DocPerm matrix fail-closed and `assert_locked` fails the migrate on drift.
- **S.4** No FE DOM hijacks — no `MutationObserver`/`innerHTML`/`eval`/`v-html` into other components.
  Prefer CSS scoping over JS DOM toggles (see C.25).
- **S.5** Secrets in env vars / Password fields ONLY — this repo is PUBLIC. Never commit a secret.

## Mechanism (codified — don't rediscover each time)
- ALL customization registers in `hooks.py` (`doc_events`, `scheduler_events`, `override_*`, `fixtures`,
  `after_migrate`, `before/after_request`).
- **Three data tiers, never mixed:** (1) schema-as-code (doctype JSON + fixtures) — auto on `bench migrate`;
  (2) intrinsic reference data (India cities) — `after_migrate` seeds; (3) business/master data —
  operator-run `db-seeds/` SQL. **Config values & secrets are operator-entered in Settings forms — never
  seeded, never hardcoded. NEVER stuff schema or data into `seeds.py`.**
- `patches.txt` = pre/post model-sync migrations. `install-app` BASELINES it WITHOUT running it, so
  structural patches ALSO re-run idempotently on `after_migrate` (`schema_setup`).
- `after_migrate` is a 10-step ORCHESTRATION with **3 gates that ABORT the migrate on registry drift**
  (automation drift · notification drift · lockdown). A migrate failure there = a missing registry row
  (code), not a flaky deploy. The automation engine = 30 `CRM Tatva Automation` toggles (auto-seeded
  DORMANT); rules are user-built. Full detail → `CICD.md` and `db-seeds/INDEX.md`.

## Deploy posture (two lanes — full detail in CICD.md)
- **Lane 1 — automatic:** build image (per-env apps file — `apps.uat.json`/`apps.prod.json` pin the fork's
  branch `uat`/`prod`; `apps.json` = `develop`; 9 apps + fork) → install → `bench migrate` (applies
  doctypes/fixtures/patches/`after_migrate`) → `enable-scheduler`. Code carries NO business values.
- **Lane 2 — manual operator:** run `db-seeds` (ordered, idempotent) → publish handbook → fill Settings
  forms + flip `CRM Tatva Automation` toggles → LSQ data migration. Nothing fires until toggled.
- Dev-first always; prove on `.devbench` before prod. Docs (`api-docs/`) ship separately (`deploy-docs.sh`),
  never on the image. Dev litter NEVER promotes — only the committed repo + `db-seeds/` SQL.

## B. How to work with me
- Do EXACTLY the narrow ask — never extrapolate to adjacent changes. STOP AND ASK before destructive/
  out-of-scope work; but once I say act, ACT — don't re-ask.
- Be confident, don't flip-flop. Don't stop short — verify yourself (read code/DB/API), don't punt.
- Simple English, lead with the answer, no closing-summary padding. Exact commands for infra.
- "Update and stop" / "no code" / "just explain" / "investigate" mean literally that.

## C. UI invariants — frontend / CRM fork (non-negotiable; hard-won)
**Reuse & structure**
- **C.1** Reuse native primitives (`ListView` family, `EmptyState`, `FormControl`, `Popover`, `Dialog`,
  `Filter`/`SortBy`/`ColumnSettings`, `useActiveTabManager`, `formatDate`). A parallel impl is a defect.
- **C.2** Build on the existing lifecycle/state path — same Pinia setup-stores, `createResource`, router.
  Invent on top of, never beside.
- **C.12** Drive a doctype-coupled native control off custom data via ONE guarded `fields` prop (`// TATVA:`),
  100% stock when absent. A parallel filter/column/sort UI is a defect.
- **C.15** Where native UX is wrong, build a NEW generic, prop-driven component (no hardcoded fields/doctype)
  on frappe-ui + `createResource`, in `tatva/`, named generically. Building new ≠ reinventing.
- **C.26** Cross-surface logic uses the ONE shared brain (composable/endpoint) every surface imports —
  never a second copy. UI thin, brain shared.

**Data & lifecycle**
- **C.3** Mount lifecycle & 1× API. `router-view` keyed on `$route.fullPath` → any path change remounts.
  Never `router.replace`/redirect on mount; never pair `auto:true` with `watch→reload()`; one
  `createResource` per component. Verify call count is 1×.
- **C.4** Bind to `resource.data` — never copy into refs in `onSuccess` (cache hits skip it → stale).
  Expose rows/columns/total as `computed`.
- **C.14** One remount source only — never stack `:key`s (an extra `:key` double-mounts → double fetch).

**Layout & theme**
- **C.5** Empty/loading/error = native `EmptyState` in a FULL-HEIGHT container (`flex flex-1 flex-col`/`h-full`).
  Never a bare line or fixed `min-h`.
- **C.6** Lay out with CSS, not JS — flexbox (`flex-1 basis-0 min-w-0` share+truncate, `shrink-0` pinned).
  No `ResizeObserver`/`offsetWidth`. (Sole JS exception: soft-keyboard insets, C.24.)
- **C.7** Theme tokens: `ink-*` is text-only (`border-ink-*` = light gray); theme-flipping surface/line =
  `surface-*`. Verify computed color in BOTH themes. Never hardcode hex (except a documented brand value).
- **C.16** Native toolbar shape: ONE horizontal row, controls right-aligned (`ml-auto flex items-center gap-2`).
  Never `flex-wrap` a toolbar. Don't repeat the record count.
- **C.17** Tight flat DOM — frappe-ui `Button`/`Dialog#actions` are chunky; use plain `<button>`+`FeatherIcon`
  for tight controls, render tight footers inside `#body-content`. Builder layouts stack on mobile.
- **C.18** frappe-ui `ListView` can't freeze/pin columns (CSS grid, no sticky hook, upstream npm). Skip
  pinning unless scoped; keep the column model a plain ordered list.

**Modals & overlays**
- **C.13** No popover-based controls inside a modal (they teleport out + fire `interact-outside` → close).
  Render inline in a modal; popovers belong on a list toolbar.
- **C.20** ONE bottom-sheet behaviour — content-fit (`max-h-[90dvh]`, `height:auto`); body scrolls past the
  cap. Fixed height + capped content = dead white band. Drag = GPU `translateY`, handle-scoped.
  `tatva/TatvaBottomSheet`, no variants.
- **C.21** Teleported popovers vs custom overlays — own the z-index with ONE rule scoped by media query
  (raise `[data-reka-popper-content-wrapper]` above the overlay), never per-control.
- **C.22** Mobile sheets adopt via the `ResponsiveDialog` wrapper (tag swap from `Dialog`); desktop stays
  byte-for-byte stock. Forward `$attrs` + every slot.
- **C.23** A modal can MOUNT already-open — sync open-state with `{ immediate: true }` or scroll-lock/
  keyboard never wire.
- **C.24** Soft keyboard: lift a `fixed bottom-0` sheet with `visualViewport` (inset by keyboard height,
  `scrollIntoView` the field). Progressive enhancement — the one sanctioned JS-for-layout exception.

**Mobile & verification**
- **C.19** MOBILE-FRIENDLY IS NON-NEGOTIABLE (CODE RED). Verify at ~390px on EVERY UI change. Fixes:
  `truncate` child won't shrink ⇒ `min-w-0` on row AND cell; overflowing chips ⇒ `flex-wrap gap-2`;
  padding/gap ⇒ `px-3 sm:px-10`, `gap-2 sm:gap-4`; scroll-strip resets ⇒ `scrollIntoView({inline:'nearest'})`.
  The native Calls/Comments timeline is the reference.
- **C.9** PWA stale-cache discipline before EVERY verification: after `bench build`, unregister the SW,
  clear `caches`, navigate away+back. Confirm live DOM has the new classes (computed style).
- **C.10** Verify with evidence, not belief — fresh proof from THIS build (post-cache-bust screenshot,
  network count, computed style). No "should work".
- **C.11** Header-less requests (`sendBeacon`, raw form posts) MUST carry `csrf_token` in the body or 500.
- **C.25** No DOM hijacks (no `MutationObserver`/`innerHTML`/`eval`/`v-html` into other components). CSS
  scoping over JS DOM toggles. `body` overflow scroll-lock + native `scrollIntoView` (reverted) are fine.

## Keep these docs current
**CLAUDE.md, AGENTS.md (its mirror), CICD.md, README.md, and `docs/INVENTORY.md` are authoritative and
MUST be updated in the same change whenever code touches their content.** Stale authority is a defect.

## Index
| Need | File |
|---|---|
| Every doctype, override, hook, endpoint | `docs/INVENTORY.md` |
| Deploy posture — lanes, automated vs manual, why | `CICD.md` |
| Step-by-step prod runbook | `docs/prod-deploy/DEPLOY.md` |
| Seed order, feature map, config activation | `db-seeds/INDEX.md` |
| Migration order | `tatva_connect/patches.txt` |
| The CRM fork + its divergence | `DHSPL-Tatvacare/frappe_tatva_crm` (branches `develop`/`uat`/`prod`) → `CUSTOMIZATIONS.md` |
| Strategy, decisions, live state | vault `tatvacare-obsidian/Projects/frappe-crm/` |
