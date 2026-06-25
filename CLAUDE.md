---
title: CLAUDE.md — tatva_connect repo constitution
tags: [frappe-crm, claude, repo-rules]
status: authoritative
updated: 2026-06-11
audience: any LLM or engineer working in this repository
---

# CLAUDE.md — working in this repo

This repo (`frappe_tatva_connect`, **public**: github.com/devops-tatvacare/frappe_tatva_connect) is the **code home** for the TatvaCare Frappe CRM: the custom app **`tatva_connect`** (WATI WhatsApp, Acefone telephony, intake forms, Azure storage, overrides) **and** the API docs site (`api-docs/`).

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
frappe_tatva_connect/                 # install with: bench get-app <repo-url>
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

1. **Thin fork of `frappe/crm` ONLY — never frappe core, never `frappe_whatsapp`, never any other upstream app.** The CRM frontend is forked to **`devops-tatvacare/crm`** (our repo, branch `tatva`, pinned to an upstream release tag) because the SPA exposes no clean client hook for list/task interactions and reliability for 300+ users cannot rest on DOM guessing. **The fork stays LEAN: it carries ONLY thin, additive "extension points"** (a handful of guarded hooks that hand the exact Vue `task`/`doc` object to `tatva_connect` — e.g. `AllModals.updateTaskStatus`, `showTask`). **ALL logic lives in `tatva_connect`** — the fork holds no business logic. Every touched upstream file gets a `// TATVA:` marker + an entry in the fork's root `CUSTOMIZATIONS.md` (file · why · upstream line), so divergence is one `grep` away and cherry-picking upstream stays trivial. Each hook is **guarded** so the CRM behaves 100% stock when `tatva_connect` isn't loaded.
   - **Order of preference is UNCHANGED — the fork is the LAST resort, not the first.** Always reach first for, in order: `override_doctype_class` → `override_whitelisted_methods` → `doc_events`/`scheduler_events` → Custom Field/Property Setter fixtures → CRM Form Script → enable a native slot and override its backend. **Add a CRM-fork extension point ONLY when the framework genuinely exposes nothing** and the alternative would be a DOM/title/order hack. When in doubt, STOP and surface it before editing the fork.
   - **Updates: cherry-pick from upstream into our `tatva` branch — never push back to `frappe/crm`.** We pull CRM from our own fork. New upstream features are cherry-picked in deliberately.
   - **Defense in depth survives the fork:** server-side fail-closed backstops (e.g. `enforce_location` / `enforce_activity_logged`) stay — a broken or absent frontend hook can never let bad data save on any path.
2. **Single source of truth = this repo. CODE ONLY IN THE REPO — NEVER TOUCH A BENCH FOLDER WITHOUT EXPLICIT PERMISSION.** All coding — for `tatva_connect` AND the CRM fork — happens in the proper git repo clone (this repo, or a clean clone of `devops-tatvacare/tatva_frappe_crm`). The local bench (`frappedev-frappe-1`, `apps/*`) is a disposable *copy* used to **run/verify** only; changes take effect there only after you sync them in. **NEVER edit, commit, push, reset, checkout, stash, or otherwise mutate any bench app folder for coding purposes without the user's explicit, per-action say-so** — not even "just to test." If the repo clone is broken/missing, STOP and tell the user; re-clone the repo — do not silently fall back to working out of the bench. Read-only inspection of the bench to diagnose runtime is fine; mutating it is not.
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
15. **File privacy is fail-closed.** Every attachment is **private** unless its doctype is a `*Settings` doctype (logos) or operator-listed public in **CRM Azure Storage Settings → Public Attachment Doctypes**. Never hardcode a privacy doctype list — a forgotten patient doctype must default private, never leak.
16. **No best guesses on inbound attribution.** Inbound messages/calls attach ONLY to a lead matched on **phone + the receiving account's grouping** (WhatsApp token / Acefone DID). No hit ⇒ **drop** (and log) — never fall back to first/most-recent/any-lead-by-phone.
17. **Smart Views are 100% user-built — NEVER seeded.** Ship the engine (doctype, composer, catalog, grain entitlement, the authoring UI), not a single `CRM Smart View` record. No `is_standard` seed, no `after_migrate`/patch/fixture/db-seed that creates a view, no "starter" or "default" view of any kind — on a fresh DB the Smart Views tab starts **empty** and every view that ever exists was authored by a user through the editor. The only thing that exists in code is the machinery; the content is always the operator's/user's. (Stronger than invariant 4: a smart view is never "structurally intrinsic" — there is no exception.)

### B. How to work with me

- **Do EXACTLY the narrow ask. Never extrapolate to adjacent changes.** A narrow instruction means *only* that — don't clean, refactor, or remove neighbouring things.
- **STOP AND ASK before anything destructive or out of scope** (deleting data, removing fields, touching prod, big refactors). **But once I say act, ACT** — don't keep re-asking.
- **Be confident. Don't flip-flop or back down.** Take the right engineering call and commit to it.
- **Don't stop short or defer.** Deliver the whole thing. If something "needs verifying," verify it yourself (read code, check the DB, research the API) — don't punt it back to me.
- **Don't burden me with decisions you can make.** If the answer is determinable, decide it.
- **Simple English, tight, lead with the answer.** No jargon walls, no option matrices, no closing-summary padding. Explain *why* in one line. Give exact commands for infra (I'm product-fluent, not DevOps).
- **"Update and stop" / "no code" / "just explain" / "investigate" mean literally that** — no verification runs, no screenshots, no "let me also fix this."

### C. UI constitution — frontend / CRM fork (non-negotiable)

Hard-won from the Smart Views + lead-tab work. Every UI change obeys these. Breaking the letter is breaking the spirit.

1. **Reuse native primitives — reinvention is a defect, not a feature.** Before building ANY UI, find the native frappe-ui / CRM-fork primitive and use it: `ListView`/`ListHeader`/`ListRows`/`ListRowItem`/`ListFooter` (bounded by `mx-3 sm:mx-5`, never full-bleed), `EmptyState`, `FormControl`, `Popover`, `Dialog`, `Filter.vue`, `ColumnSettings.vue`, native sort, `useActiveTabManager`, `formatDate`. If a primitive exists, USE IT. A parallel implementation of something native is a defect.
2. **New screens build on the EXISTING lifecycle/state path — don't invent a parallel one.** Same Pinia setup-stores (clone `stores/views.js`), same `createResource` data layer, same router patterns. If you must invent, invent *on top of* the existing path, never beside it. No bespoke fetch/store/state systems.
3. **Mount lifecycle & 1× API (the double-fetch killer).** `router-view` is keyed on `$route.fullPath` (App.vue) → every page remounts on any path change. Therefore: **(a)** never `router.replace`/redirect on mount (it remounts the page → double-fetch); let getters default to the first item. **(b)** Never pair `auto:true` with a `watch(prop) → reload()` — when the prop settles a tick later you fetch twice; use ONE trigger (an `{immediate:true}` watch that fires when the prop is ready, OR `auto`, never both). **(c)** Per-view data = one `createResource` per mounted component; **verify the call count is 1× in the network panel** before claiming done.
4. **Bind to `resource.data` — never copy into refs in `onSuccess`.** A `cache`-keyed `createResource` serves cache hits *without firing `onSuccess`*, so copied refs go stale (symptom we hit: count badge `32` but `0` rows). Expose `columns`/`rows`/`total` as `computed` off `list.data`.
5. **Empty / loading / error = the native `EmptyState` in a FULL-HEIGHT container.** Never a bare centered text line, never a fixed `min-h-[Npx]` (it collapses and the message glues to the top). The whole chain (`flex flex-1 flex-col` / `h-full`) must carry height down so `EmptyState` centers like every other tab.
6. **Lay out with CSS, not JavaScript.** Tab strips / bars: flexbox — `flex-1 basis-0 min-w-0` to share + truncate, `shrink-0` for pinned controls. No `ResizeObserver`/`offsetWidth` measuring, no store-driven widths, no fit math. The element that must flex must BE the flex child — do not wrap it in a component (e.g. `Tooltip`) that isn't a shrinkable flex child; use a native `title` instead.
7. **Theme-aware tokens, and know which is which.** `ink-*` is **text-only** — `border-ink-*` silently renders light gray. For a theme-flipping line/surface (black in light / white in dark) use `surface-*` (e.g. the native tab indicator `bg-surface-gray-7`). **Verify the computed color in BOTH themes.** Never hardcode hex (sole exception: a documented brand value like the Google map-marker blue).
8. **Never gate a tab/layout on a value you blank-then-resolve.** Toggling an async boolean to `false` and back makes the gated tab vanish→reappear, and native managers (`useActiveTabManager`) reset the active tab to the first → the indicator jumps. Memoize the decision per key so revisits are synchronous; keep the previous value until the real answer lands.
9. **PWA stale-cache discipline — MANDATORY before every verification.** The CRM is a PWA (workbox service worker) that serves old chunks. After EVERY `bench build`: unregister the SW, clear `caches`, and navigate to a different page then back BEFORE looking. Confirm the live DOM has your new classes (read computed style/`getBoundingClientRect`), not just the screenshot. A screenshot taken without a cache-bust is worthless — this trap wasted hours.
10. **Verify with evidence, not belief.** Every UI claim needs fresh proof from THIS build: a post-cache-bust screenshot, the network call count, and/or the computed style. No "should work", no claiming "fixed" off a stale render.
11. **Header-less requests (`sendBeacon`, beacons, raw form posts) MUST carry `csrf_token` in the body.** Frappe requires a CSRF token on every *authenticated* POST. `navigator.sendBeacon` sends the session cookie but **cannot set the `X-Frappe-CSRF-Token` header**, so put `csrf_token: window.csrf_token` in the JSON body — Frappe reads it from `form_dict` when the header is absent. Without it the call **500s on every fire** (e.g. presence `mark_away` on tab-hide).
12. **Reuse a doctype-coupled native control over custom data with a GUARDED `fields` prop — never fork its logic.** `Filter.vue`/`ColumnSettings.vue`/`SortBy.vue` fetch fields by doctype (`get_filterable_fields`/`getMeta`/`sort_options`). To drive them off a custom catalog, add ONE optional prop (`fields`/`fieldSource`) marked `// TATVA:` that, when non-empty, replaces the meta fetch and is **100% stock when absent**; feed it `{fieldname, fieldtype, label, options}`. This is the only non-DOM way to reuse them — building a parallel filter/column/sort UI is a defect.
13. **Don't put popover-based controls inside a modal — build an INLINE control instead.** `Filter`/`SortBy`/`ColumnSettings` (any frappe-ui `Popover`) teleport their dropdown OUTSIDE the dialog panel, so in a modal they cascade over the background AND fire reka-ui `interact-outside` (closing the dialog). Popover controls are for a **list toolbar** (their home). For a modal build-journey render the UI INLINE (see `tatva/ConditionBuilder.vue`, `tatva/ColumnManager.vue`). If a teleporting control truly must live in a Dialog, set `disableOutsideClickToClose` (and accept it'll still look detached).
14. **One remount source only — never stack `:key`s.** `App.vue` keys `<router-view>` on `$route.fullPath`, so a route-param-driven view already remounts the whole page on any change. An additional `:key` on a child (e.g. the list keyed on the active view) double-mounts → **double API fetch** — visible when an async step (`await store…reload()`) splits the route change across ticks. Let the page remount be the single refresh trigger. (Verify on mount: 1× per-view fetch, no fan-out, no redirect.)
15. **Reuse native when it fits; otherwise build a NEW GENERIC component on the native lifecycle — that is allowed.** Reuse only where a native primitive genuinely fits (the list toolbar reuses Filter/Sort/ColumnSettings as popovers — correct there). Where the native UX is wrong for the context (popovers in a modal; a chip list where LSQ wants a two-panel picker), build a NEW component: **generic, prop-driven (no hardcoded fields/doctype), on frappe-ui controls + `createResource`**, in `tatva/`, named generically (`ConditionBuilder`, `ColumnManager` — NOT `SmartView*`) so it's reusable. Building new ≠ reinventing; reinventing is duplicating a native primitive that already fits.
16. **Match the native toolbar shape: ONE horizontal row, controls right-aligned (`ViewControls.vue`).** Never `flex-wrap` a toolbar — it stacks into a vertical column (a defect we shipped). Search left; Filter/Sort/Columns (+ a discoverable Edit-view affordance) in an `ml-auto flex items-center gap-2` group. **Don't repeat the record count in the toolbar** — it's already on the tab and in the `ListFooter`. Editing a saved view needs a visible entry point (a toolbar button), not only a hover-hidden menu item.
17. **Tight, flat DOM — frappe-ui `Button` and `Dialog#actions` are chunky.** A frappe-ui `Button` forces ~28px height/padding; for a tight in-row control (e.g. a card's ✕) use a plain `<button>` + small `FeatherIcon`. The Dialog `#actions` slot wraps the footer in `pt-4 + pb-7` over the body's `pb-6` — a dead ~40px band; for a tight footer render it INSIDE `#body-content` with your own `mt-*`. Drop wrapper divs that do nothing (`min-h-0 flex-col`). Two-panel/builder layouts MUST stack on mobile (`grid-cols-1 sm:grid-cols-2`) — PWA-compat is mandatory, verify at ~390px.
18. **frappe-ui `ListView` cannot freeze/pin columns.** It's a CSS grid (`getGridTemplateColumns` off each column `width`); the cell `<div>`s belong to frappe-ui `ListRow` with no sticky/`position` hook, and `frappe-ui` is an upstream npm package (not editable). Real frozen columns need forking the table or a custom grid — skip pinning unless explicitly scoped, and keep the column model a plain ordered list so pin can layer on later with no churn.

---

## Deploying invariants

1. **Dev-first, always.** Build and prove on the local `.devbench`; deploy to prod only after local proof. Never experiment on prod. On dev, keep WATI + Acefone + follow-up kill-switches OFF; use only the owner number for test leads.
2. **Two lanes, kept separate:** **app code** (`tatva_connect`) → bake into the prod image (`apps.json` → rebuild → `install-app`/`migrate`/`build`); **docs** (`api-docs/`) → fast file push (`deploy-docs.sh`), **never** rides the image rebuild.
3. **Git:** feature branch → fast-forward into `main` → push. Pushing to GitHub does **not** touch prod. Commit only when a slice is proven, or when asked.
4. **Only clean code + documented config promote — dev litter NEVER moves.** What goes to prod is exactly (a) the committed repo and (b) the explicit `db-seeds/` SQL + cutover plan. If it works on dev but isn't in the repo or the plan, it does not exist for prod — re-create it cleanly, never copy the bench.

---

## Local dev flow — update `tatva_connect` on the devbench (git-based, NO rsync/copy)

The devbench `apps/tatva_connect` is a **real git clone** of this repo (`origin` = `github.com/devops-tatvacare/frappe_tatva_connect`, branch `main`). The **GitHub repo is the source of truth** — never rsync/copy the working tree into the bench. Loop: **edit → commit → push to `main` → pull on the devbench.**

**Environment:** container `frappedev-frappe-1` · bench `/workspace/development/frappe-bench` · site `dev.localhost`.

**One-shot update (run from the Mac):**
```bash
docker exec frappedev-frappe-1 bash -lc '
  cd /workspace/development/frappe-bench/apps/tatva_connect &&
  git fetch origin main && git reset --hard origin/main &&
  cd /workspace/development/frappe-bench &&
  bench --site dev.localhost migrate &&     # runs after_migrate: schema_setup + seeds + form/client-script re-seed
  bench build --app tatva_connect           # only if public/ JS|CSS changed
'
docker exec frappedev-redis-cache-1 redis-cli FLUSHALL                 # drop stale boot / module-map cache
docker exec -d frappedev-frappe-1 bash -lc 'cd /workspace/development/frappe-bench && bench start'  # if dev server is down
```

**When to run what:**
- Python / hooks / doctype JSON / fixtures / seeds changed → **`migrate`** (re-runs `after_migrate`).
- `public/` JS or CSS changed → **`bench build --app tatva_connect`**.
- CRM Form Scripts (`*/form_scripts/*.js`) changed → they **re-seed on `migrate`** (no build).
- Change didn't take → **flush redis-cache + restart `bench start`** (the dev server caches boot + module map in memory; a console `clear_cache` doesn't clear the running worker).

**Rules:** `git reset --hard origin/main` is safe (the bench clone is disposable; repo is the only truth — per the "dev litter never moves" invariant). Never hand-edit files inside the bench clone — edit in the repo, commit, push, pull. `bench start` is the dev web server; if it stops the whole site is down.

> Prod is the *other* lane (image rebuild via `apps.json`) — see the v16 runbooks. This section is **local dev only**.

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
