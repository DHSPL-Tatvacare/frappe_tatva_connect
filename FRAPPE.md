# FRAPPE.md — Frappe Best Practices Reference

Living reference of Frappe framework best practices, compiled from upstream docs, frappe_docker, and
hard-won lessons. Use as a checklist for new work and code review.

---

## 1. App Structure & Packaging

- **`pyproject.toml` with `flit_core`** (v15+ standard, NOT `setup.py`). Dependencies listed under
  `[project] dependencies`; bench-managed packages (frappe itself) commented out.
- **`required_apps`** in `hooks.py` declares hard dependencies — enforces install ordering so your
  fixtures/Custom Fields on a dependency's doctypes never abort.
- **`modules.txt`** registers every module; the app's module list lives here.
- **Standard directory layout**:
  ```
  app_name/
  ├── hooks.py              # ALL integration points
  ├── modules.txt            # module registry
  ├── patches.txt            # database migrations
  ├── seeds.py               # after_migrate intrinsic seeds
  ├── schema_setup.py        # after_migrate structural patches (idempotent re-runs)
  ├── fixtures/              # Custom Field, Property Setter, Dashboard Chart, Number Card JSON
  ├── <module>/doctype/<dt>/ # one folder per doctype (JSON + .py controller)
  ├── <module>/workspace/<slug>/  # workspace JSON (standard file, NOT fixture)
  ├── workspace_sidebar/     # workspace sidebar JSON (standard file, NOT fixture)
  ├── api/                   # @frappe.whitelist() endpoints
  └── public/{js,css}/       # frontend assets (built via `bench build --app`)
  ```
- App name must match the package folder (`snake_case`).

---

## 2. The Extension Ladder (override order of preference)

**Reach for these in order — the fork is the LAST resort, not the first.**

| Priority | Mechanism | When |
|----------|-----------|------|
| 1 | `override_doctype_class` | Replace an entire doctype controller (form saves, fetches, methods) |
| 2 | `override_whitelisted_methods` | Redirect a specific whitelisted endpoint to your own implementation |
| 3 | `doc_events` | Validate, transform, or react to document lifecycle (no controller swap) |
| 4 | Fixtures (Custom Field / Property Setter) | Add fields, change field properties, hide fields — no code |
| 5 | CRM Form Script / Desk Client Script | UI-only behaviour in the browser |
| 6 | Native slot (if upstream exposes one) | Plug into a deliberate extension point without a fork |
| 7 | **Fork upstream (LAST RESORT)** | Only when the framework genuinely exposes nothing |

### 2a. `override_doctype_class`

- Subclass the existing controller, override only the method(s) you need.
- **Fall through to `super()` for stock behaviour** on unrelated cases.
- Keep the override file minimal — no business logic that can live in a separate module.
- Example pattern:
  ```python
  class WATIWhatsAppMessage(WhatsAppMessage):
      def send(self, *args, **kwargs):
          if not self._should_route_wati():
              return super().send(*args, **kwargs)  # stock for non-WATI
          return self._send_via_wati(*args, **kwargs)
  ```

### 2b. `override_whitelisted_methods`

- Dict mapping `"original.module.path.method": "your.module.path.method"`.
- **Import the native function directly** to delegate after your gate — never dispatch through
  Frappe (creates recursion). Pattern:
  ```python
  @frappe.whitelist()
  def get_assigned_users(doctype, name, default_assigned_to=None):
      frappe.has_permission(doctype, "read", name, throw=True)
      from crm.api.doc import get_assigned_users as _native
      return _native(doctype, name, default_assigned_to)
  ```
- Only wrap methods that genuinely bypass the permission engine (`get_all`, `ignore_permissions`,
  un-gated `get_doc`). Methods that already check permissions don't need a redundant wrapper.
- The override key is the **full dotted path** to the original whitelisted function, not a doctype path.

### 2c. `doc_events`

- Scoped per doctype + event: `validate`, `before_save`, `on_update`, `after_insert`, `on_trash`, etc.
- Wildcard (`"*"`) is available but must have a **cheap cached guard** at the top to early-return
  for non-matching doctypes — never run logic on every document save.
- Event ordering within a list matters: e.g. `seed_checklist` before `enforce_checklist` on validate.
- Use `after_insert` + `enqueue_after_commit=True` for async side effects that shouldn't block the save.

### 2d. Fixtures (see §3)

### 2e. Fork upstream (LAST resort)

- The fork stays LEAN: **only thin additive extension points**, no business logic.
- Every touched file gets a `// TATVA:` marker + an entry in the fork's `CUSTOMIZATIONS.md`.
- Each hook is **guarded** (CRM behaves 100% stock when your app isn't loaded).
- Cherry-pick from upstream into your fork's branch; never push back to upstream.

---

## 3. Fixtures — Schema-as-Code

- **Structural records only**: Custom Field, Property Setter, Dashboard Chart, Number Card.
- **Always use filters** to scope exports — never vacuum an entire doctype's records.
- Filter patterns:
  ```python
  # By doctype (for fields WE own on native doctypes)
  {"dt": "Custom Field", "filters": [
      ["dt", "in", ["CRM Lead", "CRM Task"]],
      ["fieldname", "!=", "workflow_state"],  # exclude Frappe-managed fields
  ]},
  # By name (for shared doctypes where we own only specific fields)
  {"dt": "Custom Field", "filters": [["name", "in", ["WhatsApp Message-custom_failed_reason"]]]},
  # By name for dashboards
  {"dt": "Dashboard Chart", "filters": [["name", "in", ["Chart A", "Chart B"]]]},
  ```
- **Business/config data is NEVER a fixture** — it goes in `db-seeds/` (gitignored, operator-run SQL)
  or is entered by the operator through Settings forms.
- **Workspaces ship as standard files**: `<module>/workspace/<slug>/<slug>.json` and
  `workspace_sidebar/<name>.json`. Frappe's `remove_orphan_entities()` prunes fixture-only
  workspaces on every migrate — standard files survive.
- Dashboard Charts and Number Cards **cannot** ship as module-folder standard files (they're not
  in Frappe's `IMPORTABLE_DOCTYPES`) — they must be fixtures.
- `Custom Field` across multiple doctypes can share a fixture entry; use `"dt", "in", [...]` filter.

---

## 4. Patches & after_migrate

### 4a. Patches (`patches.txt`)

- **`[pre_model_sync]`**: Schema changes that MUST happen BEFORE doctype JSON syncs (renames,
  column carries before drop, field repoints).
- **`[post_model_sync]`**: Data migrations that run AFTER doctype sync (indexes, Select option
  appends, data backfills).
- **Every patch must be idempotent** — guard with column-exists/row-exists/value-exists checks
  so re-running is a no-op. Pattern: each patch has an `execute()` function.
- **Archive dead patches**: comment them out in `patches.txt` with archive paths — never silently
  delete. Keep the migration history legible.
- **Patch files go in `tatva_connect/patches/`** (auto-discovered by Frappe).

### 4b. `after_migrate` — the Fresh-DB Gap

- **CRITICAL**: `install-app` baselines `patches.txt` WITHOUT running it. Any structural change
  that needs to exist on a fresh DB must ALSO run on `after_migrate` (idempotently).
- Ordering matters:
  1. Schema patches (indexes, column drops, Select appends)
  2. Lockdown (permission matrix rebuild)
  3. Catalog sync (automation registry rows)
  4. Drift assertions (fail if registry/permissions drifted)
  5. Form/client script re-seeds
  6. Intrinsic master-data seeds (cities, side-effect options)
- Each step is isolated — one failure never aborts the migrate (rollback + log + continue).
- Seeds run AFTER fixtures so Linked masters (Vertical/Group) exist.

### 4c. What goes WHERE (decision matrix)

| Change | Mechanism | Why |
|--------|-----------|-----|
| New Custom Field on own doctype | doctype JSON | Standard; syncs on migrate |
| New Custom Field on upstream doctype | fixture JSON | Can't edit upstream JSON; fixture auto-applies |
| Change fieldtype/length/hidden on upstream field | Property Setter fixture | Non-destructive overlay |
| Append option to upstream Select | `schema_setup.py` + `patches.txt` | Merge-not-clobber; fixture would REPLACE options |
| Composite index on upstream doctype | `schema_setup.py` + `patches.txt` | Can't declare in upstream JSON |
| Rename a column BEFORE JSON sync | `[pre_model_sync]` patch | Data must carry forward before the column drops |
| Backfill data AFTER new columns exist | `[post_model_sync]` patch | Columns must already exist |
| Drop obsolete columns | `schema_setup.py` + `patches.txt` | Idempotent, safe on fresh DB |
| Seed intrinsic reference data | `seeds.py` (on after_migrate) | Identical in every deployment |
| Seed business/master data | `db-seeds/` SQL (operator runs) | Differs per deployment |

---

## 5. hooks.py — Complete Registration Surface

### 5a. Standard metadata
```python
app_name = "my_app"
app_title = "My App"
app_publisher = "Company"
app_description = "..."
required_apps = ["crm"]
```

### 5b. `doc_events`
```python
doc_events = {
    "CRM Lead": {
        "before_validate": ["my_app.lead.validate.prepare"],
        "validate": ["my_app.lead.validate.step_a", "my_app.lead.validate.step_b"],
        "on_update": ["my_app.lead.events.on_update"],
    },
    "*": {
        "after_insert": "my_app.common.guarded_handler",  # cheap guard required
    },
}
```
- List order for the same event matters (executed in order).
- Wildcard `*` handlers MUST have a cheap early-return for non-matching doctypes.

### 5c. `permission_query_conditions`
```python
permission_query_conditions = {
    "CRM Task": "my_app.tasks.permissions.get_task_pqc",
}
```
- Returns a SQL AND clause (string) appended to every list query.
- `""` means no extra scoping (stock behaviour).
- Use `frappe.db.escape()` on user-supplied values injected into SQL.

### 5d. `has_permission`
```python
has_permission = {
    "CRM Task": "my_app.tasks.permissions.has_task_permission",
}
```
- Returns `True` to grant, `False` to deny. **v16+ requires explicit boolean** (truthy values
  no longer work).
- Controls single-doc access (deep links, direct opens) — the PQC only governs lists.

### 5e. `scheduler_events`
```python
scheduler_events = {
    "cron": {
        "0 */6 * * *": ["my_app.tasks.sync_all"],
        "30 2 * * *": ["my_app.tasks.daily_cleanup"],
    },
}
```
- Use cron format for precision; also supports `"daily"`, `"hourly"`, `"weekly"`, `"monthly"`,
  `"all"` (every minute).
- Long-running jobs: `enqueue_after_commit` inside the handler, keep the cron callback fast.

### 5f. `before_request` / `after_request`
```python
before_request = ["my_app.observability.stamp_start"]
after_request = ["my_app.observability.log_request", "my_app.api.normalise_errors"]
```
- Runs on EVERY HTTP request — must have cheap guards for non-relevant endpoints.
- `after_request` fires after the response status is final; good for logging/normalisation.

### 5g. `fixtures` (see §3)

### 5h. `after_migrate` (see §4b)

### 5i. Other hooks
- **`app_include_js`** / **`app_include_css`**: Shared desk assets loaded on every Desk page.
- **`add_to_apps_screen`**: Desk app-switcher tile with `has_permission` gate.
- **`website_generators`**: Auto-create web pages from doctype records.
- **`website_theme_scss`**: Custom SCSS for website themes.

---

## 6. Whitelisting API Methods

### 6a. Standard whitelisting
```python
@frappe.whitelist()
def my_endpoint(arg1, arg2=None):
    frappe.has_permission("My DocType", "read", throw=True)
    return frappe.get_doc("My DocType", arg1)
```
- Every method callable from frontend (`frappe.call`, `frm.call`) must have `@frappe.whitelist()`.
- Methods on doctype controllers use `@frappe.whitelist()` on the method inside the class.
- **Every whitelisted method must check permissions internally** — the decorator does NOT gate access.

### 6b. Guest endpoints
```python
@frappe.whitelist(allow_guest=True)
def webhook_handler(payload):
    ...
```
- Used for inbound webhooks (WhatsApp, telephony), public forms.
- Must implement its own authentication (token comparison, HMAC, signature).
- Use `hmac.compare_digest()` for constant-time token comparison.

### 6c. Doctype methods called from frontend
```python
class MyDocType(Document):
    @frappe.whitelist()
    def my_method(self, arg):
        pass
```
Frontend calls:
```js
frm.call("my_method", {arg: value})
// or
frappe.call({doc: frm.doc, method: "my_method", args: {arg: value}})
```

---

## 7. Permissions — Four-Layer Defense

### Layer 1: Doctype-level (DocPerm / Custom DocPerm)
- Standard doctypes' permission matrix is defined in `Custom DocPerm`.
- When **any** `Custom DocPerm` exists for a doctype, the stock `DocPerm` is **entirely ignored**.
- Strategy: `reset_perms(doctype)` then rebuild to exactly your allowed roles — everything else is denied.
- Idempotent rebuild on `after_migrate`; assert no drift afterward.
- Pattern:
  ```python
  def apply():
      reset_perms("Contact")
      for role, (r, w, c, d) in MATRIX.items():
          frappe.get_doc({
              "doctype": "Custom DocPerm",
              "parent": "Contact",
              "role": role,
              "read": r, "write": w, "create": c, "delete": d,
          }).insert(ignore_permissions=True)
      frappe.clear_cache()
  ```

### Layer 2: Row-level list scoping (`permission_query_conditions`)
- Returns SQL fragment appended to WHERE clause on every list/report query.
- Child doctypes should inherit the parent's scope — reuse the parent's exact
  `DatabaseQuery.build_match_conditions()` so scope never drifts.
- Use `frappe.db.escape()` for user values.
- Return `""` for privileged users or when the feature switch is OFF.

### Layer 3: Single-doc gate (`has_permission`)
- Controls access by document name (deep links, direct opens).
- Must mirror the PQC logic: if a row is hidden from list, it must be unreachable by name.
- Check owner/assignee first (self-owned records always visible).
- For child documents, resolve the parent and defer to the parent's read scope.

### Layer 4: Drift assertion
- On `after_migrate`, assert that no locked doctype is open to All/Guest for write/create/delete.
- Assert that no doc_event/scheduler hook lacks a matching kill-switch registry row.
- Fail the migrate loudly — a drifted permission is a security vulnerability.

### VAPT: Guard native bypass methods
- Some upstream methods call `frappe.db.get_all()` or `get_doc()` without going through
  `frappe.has_permission()`. Neither the DocPerm matrix nor `has_permission` hooks can reach them.
- Intercept via `override_whitelisted_methods` → gate via `frappe.has_permission()` → delegate
  to the unchanged native function.
- Do NOT duplicate guards on methods that already check permissions themselves.

---

## 8. Secrets & Configuration

- **Secrets**: Use `Password` field type in Settings doctypes. For environment-level secrets,
  use environment variables read in `site_config.json` or directly in Python.
- **Secrets are NEVER committed** — the repository is public.
- **Plaintext → Password migration**: When flipping a field from `Data` to `Password`, carry
  existing values via a `[post_model_sync]` patch (read plaintext, write to Password store).
  Merge-not-clobber: the column placeholder is the idempotency guard.
- **Settings Singles**: One per integration domain — `CRM WhatsApp Settings`, `CRM Telephony
  Settings`, `CRM Azure Storage Settings`, etc.
- **All features ship DORMANT** — enabled=0 on every kill-switch row. A blank/unsaved setting
  reads as disabled. Nothing fires until an operator enables it in the form.
- **No hardcoded defaults for business values** — only structural/intrinsic defaults are
  allowed (a checkbox starting at 0, a naming series, a 0/1 numeric default).
- Litmus test: "Would two deployments legitimately set this differently?" → Yes ⇒ no default.
- **Form stays blank**; sensible behaviour lives in a **code fallback** (`value or DEFAULT`),
  never a baked form value.

---

## 9. Database

- **read replicas**: Configure in `site_config.json`:
  ```json
  {
    "read_from_replica": 1,
    "replica_host": "replica-host",
    "replica_db_name": "dbname",
    "replica_db_password": "password"
  }
  ```
- **Multi-tenancy**: One database per site. Frappe isolates sites at the DB level.
- **No write-sharding** — Frappe doesn't natively support it.
- **Composite indexes**: On upstream doctypes, declare via `schema_setup.py` + `patches.txt`
  (can't add to upstream JSON). Frappe's MariaDB index creation is `ALTER TABLE ADD INDEX`.
- **Select option appends**: Use `frappe.db.sql()` to append options to a Select field on an
  upstream doctype — a fixture would REPLACE all options and drift across upstream versions.

---

## 10. Production Deployment

- **Docker**: Use `frappe/frappe_docker` with compose files. Separate services:
  - `backend` (gunicorn)
  - `frontend` (nginx)
  - `websocket` (socketio)
  - `scheduler` (background jobs)
  - `worker` (queue processing)
- **Configuration**: Environment variables for DB passwords, hostnames. `common_site_config.json`
  for bench-wide settings; `site_config.json` for site-specific settings.
- **Backups**: Cron-based `bench backup` with optional restic snapshots for off-site storage.
- **Multi-tenancy**: Unique site services with `FRAPPE_SITE_NAME_HEADER` and distinct ports.
- **HTTPS**: Traefik or nginx-proxy with Let's Encrypt.
- **Redis**: Separate instances for cache, queue, and socketio (or sentinel for HA).

---

## 11. Code Quality

- **Linting**: ruff with `pyproject.toml` config. Standard rules: F, E, W, I, UP, B, RUF.
- **Security SAST**: Bandit on shipped code (exclude tests, archive, devbench, node_modules).
- **Type annotations**: `frappe.types.DF` as typing module for DocField types.
- **Consistent formatting**: Choose tab/space, quote style, line length — enforce via ruff.
- **Dead code**: Archive to `archive/` (gitignored), not stranded. Leave a commented trace
  in the relevant index file (patches.txt, etc.).
- **No speculative abstractions**: Three similar lines of code is better than a premature
  abstraction. Extract to a shared function when there's genuine reuse across modules.

---

## 12. Common Pitfalls

| Pitfall | Fix |
|---------|-----|
| `has_permission` returns truthy non-boolean (v16) | Return explicit `True` / `False` |
| Fixtures vacuum all records of a doctype | Always add name/doctype filters |
| Workspace shipped as fixture (pruned on migrate) | Ship as standard file in `<module>/workspace/<slug>/` |
| Patch not idempotent (fails second run) | Guard with column/row/value-exists checks |
| `install-app` gap — structure only in patches.txt | Re-run structural patches on `after_migrate` |
| `override_whitelisted_methods` dispatch re-enters override | Import native function directly, don't dispatch |
| Wildcard `doc_events` on `*` without guard | Start with cheap cached set-membership test |
| Secret committed (public repo) | Use Password field type or env var |
| Permission check missing in whitelisted endpoint | Check at top: `frappe.has_permission(..., throw=True)` |
| Select fixture REPLACES upstream options | Append in code via `schema_setup.py` |
| Double-fetch: `auto:true` + `watch(prop) → reload()` | ONE trigger: watch with `{immediate:true}` OR `auto`, never both |
| Cache-bust failure (PWA serves stale chunks) | Unregister SW, clear caches, verify DOM has new classes |

---

## 13. Quick-Reference Checklist (New Feature / Code Review)

- [ ] `pyproject.toml` dependencies declared
- [ ] `required_apps` updated if new dependency
- [ ] New doctype registered in `modules.txt` (if new module)
- [ ] Custom Fields on upstream doctypes → fixture JSON with filters
- [ ] Property Setters on upstream fields → fixture JSON
- [ ] Workspace → standard file (not fixture)
- [ ] Dashboard charts/cards → fixture (by name)
- [ ] Structural-only change → patch + `schema_setup.py` double-coverage
- [ ] Data migration → `[post_model_sync]` patch, idempotent
- [ ] Intrinsic seed → `seeds.py` (identical in every deployment)
- [ ] Business seed → `db-seeds/` SQL (NOT auto-seeded)
- [ ] All `@frappe.whitelist()` methods check permissions
- [ ] Guest endpoints have `allow_guest=True` + own auth
- [ ] Secrets in Password field or env var (never committed)
- [ ] Feature ships dormant (kill-switch row, enabled=0)
- [ ] Kill-switch registry row created
- [ ] `after_migrate` steps ordered correctly
- [ ] `has_permission` returns explicit `True`/`False`
- [ ] No wildcard hooks without cheap guard
- [ ] Override falls through to stock for unrelated cases
- [ ] `override_whitelisted_methods` imports native directly (no recursion)
- [ ] Patches archived as comments (not deleted)
- [ ] Dead code moved to `archive/` (not stranded)
