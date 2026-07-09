<!-- AUTHORITATIVE permissions policy for tatva_connect / TatvaCare healthcare CRM.
     Governs every doctype's `permissions[]` block and access/lockdown.py::LOCKED_MATRIX.
     The Desk Role Permission Manager is NEVER the source of truth — this document + the code are. -->

# Permissions Policy — TatvaCare healthcare CRM

> **TL;DR.** Access = the **union** of every role a user holds. Every login *also* silently holds the
> `All` role. So a doctype that lists `All` is open to **everyone**. Therefore: each doctype is sorted
> into one **bucket** (Operational / Config / Admin / Child / Capability); the bucket fixes the
> `(role, read, write, create, delete)` rows. We declare those rows **in code** — `permissions[]` in
> the doctype JSON for doctypes we own, `access/lockdown.py` for stock doctypes we don't. The Desk is
> for experiments only; it is never the source of truth (it is wiped on every image rebuild).

---

## 1. The mental model (plain English)

- A **Role** describes *what a user can do* on a doctype (read / write / create / delete …).
- **A user holds many roles at once**, and Frappe **auto-attaches** these to every logged-in user,
  on top of whatever you assigned:
  - `All` — **every authenticated user** (incl. website/portal users). You can't assign or remove it.
  - `Guest` — everyone, including not-logged-in.
  - `Desk User` — every backend (System) user.
- **Access is a UNION.** A user is allowed if **ANY** of their roles is allowed. There is no "becoming"
  a role — they carry all of them simultaneously.

**Worked example — "Asha", a Sales rep.** Her real role set on every request is:
`{ Sales User, All, Guest, Desk User }`.

| Doctype | Its permission rows list… | Asha allowed? | Via |
|---|---|---|---|
| `CRM Lead` | Sales User, Sales Manager, System Manager | ✅ | **Sales User** |
| `HD Ticket` | Agent, Agent Manager, **All**, System Manager | ✅ | **All** ← because she always carries `All` |
| `CRM Automation Rule` | Sales Manager, System Manager | ❌ | none of hers match |

> **The rule that matters:** putting `All` on a doctype = granting it to **literally everyone**.

---

## 2. The permission tuple (the unit you actually write)

Every permission row is a tuple. The five that matter day-to-day:

```
(role, read, write, create, delete[, permlevel, if_owner])
```

- `permlevel` (default `0`) — field-group gate. Fields with `permlevel: 1` are only seen/edited by a
  role granted at level 1. Use for sensitive fields (e.g. an approved amount).
- `if_owner: 1` — the row only applies to records the user **created/owns** ("your own ToDos").

**Example tuples (read them out loud):**

| Tuple | Means |
|---|---|
| `(Sales User, 1,1,1,0)` | rep can read/write/create leads, **not delete** |
| `(Sales Manager, 1,1,1,1)` | manager can also delete |
| `(Sales User, 1,0,0,0)` | rep can **only read** (e.g. a config master) |
| `(All, 1,0,0,0)` on `Country` | everyone can read reference data — fine |
| `(All, 1,1,1,0, 0, 1)` on `ToDo` | everyone, but only **their own** todos — fine |
| `(All, 1,1,1,0)` on `HD Ticket` | everyone can read/write/create **all** tickets — **breach** |
| `(WhatsApp User, 1,0,0,0)` on `WhatsApp Message` | read-only; sending goes through a gated method |

---

## 3. The roles we use (and the three axes they live on)

Access has **three independent axes** — never blend them:

| Axis | Question | Mechanism |
|---|---|---|
| **Capability** | *What can they do?* | **Role / Role Profile** (this doc) |
| **Scope** | *Which records?* | **grain** via Assignment-Rule membership |
| **Navigation** | *Which app menu shows?* | **Module Profile** (Desk-menu only; grants nothing) |

**Capability roles (the only ones we author perms for):**

| Role | Kind | Purpose |
|---|---|---|
| `Sales User` | bucket role | the CRM rep — operate on records |
| `Sales Manager` | bucket role | rep + curate config + manage automation/integrations |
| `WhatsApp User` | capability | read WhatsApp doctypes; send via gated path |
| `WhatsApp Admin` | capability | manage WhatsApp accounts/settings |
| `Field Map User` | capability | single gate for the Near Me page |
| `System Manager` | admin | full backstop on every doctype |

A person = **one bucket role** (Sales User/Manager) **+** capability add-ons. We never merge them into a
single mega-matrix.

---

## 4. The buckets — how to classify ANY doctype

> **The one question:** does a rep **DO** this, **CONSUME** this, or **never touch** it?
> DO → bucket 1 · CONSUME → bucket 2 · never → bucket 3.

(These bucket names are **ours**, not Frappe's — a lens. The underlying CRUD per role is the real thing,
and it is derived from how the official `frappe/crm` app already splits Sales User vs Sales Manager.)

| Bucket | What it is | Sales User | Sales Manager | System Manager | Examples |
|---|---|---|---|---|---|
| **1 · Operational** | records a rep works on daily | `1,1,1,0` | `1,1,1,1` | full | CRM Lead, Task, Note, Deal, Call Log |
| **2 · Config** | masters/layouts a rep reads, a manager curates | `1,0,0,0` | `1,1,1,1` | full | Lead Source, Fields Layout, Form Script, Settings |
| **3 · Admin** | integrations / automation / settings | — none — | `1,1,1,1` | full | Automation Rule, Telephony/WhatsApp config, SLA, Sync |
| **Child** | child table (`istable=1`) | inherits parent | inherits | inherits | the `*Profile` tables |
| **Capability** | gated by a capability role, not Sales | — | — | full | WhatsApp Message (WhatsApp User), Near Me (Field Map User) |

**Copy-paste blocks** — paste the matching one into the doctype JSON's `"permissions"` array:

```jsonc
// Bucket 1 · Operational
[ {"role":"System Manager","read":1,"write":1,"create":1,"delete":1},
  {"role":"Sales Manager", "read":1,"write":1,"create":1,"delete":1},
  {"role":"Sales User",    "read":1,"write":1,"create":1,"delete":0} ]   // drop delete from reps

// Bucket 2 · Config
[ {"role":"System Manager","read":1,"write":1,"create":1,"delete":1},
  {"role":"Sales Manager", "read":1,"write":1,"create":1,"delete":1},
  {"role":"Sales User",    "read":1,"write":0,"create":0,"delete":0} ]   // reps read-only

// Bucket 3 · Admin
[ {"role":"System Manager","read":1,"write":1,"create":1,"delete":1},
  {"role":"Sales Manager", "read":1,"write":1,"create":1,"delete":1} ]   // no Sales User row

// Child table → "permissions": []   (empty — inherits parent)
```

---

## 5. The `All`-role policy (the breach rule)

Because **every user always carries `All`**, the only safe rule is:

**`All` may appear on a doctype ONLY if one of these is true:**
1. **Read-only reference data** everyone needs — `(All, 1,0,0,0)` on Country, Language, Gender, Module Def…
2. **`if_owner`-scoped** to a user's own records — `(All, …, if_owner=1)` on ToDo, Address, ticket feedback…
3. **Framework-essential allowlist** the UI breaks without — File, ToDo, Tag, Tag Link, Notification Settings.

**`All` is FORBIDDEN** on every other doctype — all business records, automation, integrations, settings.
There, list the **specific role**, never `All`.

**Do NOT** try a global "turn `All` off" or "make `All` read-only" — it breaks core UX (File upload, ToDo
assignment, portal self-service). The fix is a **curated denylist**, doctype by doctype, enforced in
`lockdown.py`. (tatva_connect's own doctypes grant `All` **nowhere** — already compliant. The leaks are in
stock apps you install.)

**Current denylist (stock apps) — enforced in `lockdown.py::LOCKED_MATRIX`:**

| Doctype(s) | App | Why | Lock to | Status |
|---|---|---|---|---|
| `Contact` | frappe | stock grants `All` | System Manager, Sales Manager, Sales User (reps no delete) | **locked** ✅ |
| `Comment` | frappe | stock write reachable via `frappe.client.set_value` → cross-user IDOR (VAPT P1) | System Manager + Website Manager; operational roles write/delete `if_owner=1` (edit OWN only) | **locked** ✅ |
| `HD Ticket` | helpdesk | `All` read/create on *all* tickets — no customer portal here (agent-only internal) | System Manager, Agent, Agent Manager | **locked** ✅ |
| `HD Article`, `HD Article Category`, `HD Article Feedback`, `HD View` | helpdesk | `All`/`Guest` read/write on KB + views | System Manager, Agent, Agent Manager | **locked** ✅ |
| `WhatsApp Message/Templates/Account/Settings` | frappe_whatsapp | capability doctypes | WhatsApp User/Admin (read) + System Manager | **locked** ✅ |
| `TP Exotel Settings`, `TP Twilio Settings` | telephony | provider config world-readable | System Manager (+ manager) | pending |

> **Engine-bypassing methods** (Helpdesk `get_article_stats`, LMS `get_courses`/`get_batches`/
> `get_job_details`, CRM `get_assignment_rules_list`/`get_views`) can't be closed by a DocPerm lock —
> they read via `get_all`/`db.get_value`. They are wrapped in `access/native_guards.py` (Primitive A):
> a `has_permission` gate, or a metamorphic NARROW (LMS → `published=1` for non-privileged callers).
> The profile-picture private-file BAC is closed in `storage/file_override.py::FileOverride.validate`.
> Full remediation map → `docs/plans/vapt-jun26-remediation.md`.

---

## 6. Where permissions live (enforcement) — Desk is NEVER the source

| Doctype kind | Declare permissions in | Notes |
|---|---|---|
| **Our custom parent doctypes** | the `permissions[]` array in their `.json` | applied on `bench migrate`; version-controlled |
| **Our child tables** | nothing (`permissions: []`) | inherit parent |
| **Stock / core / Helpdesk (not ours)** | `access/lockdown.py::LOCKED_MATRIX` | `after_migrate` rebuild, fail-closed, idempotent |
| **Field-level** | `permlevel` on the field + one perm row at that level | for sensitive fields only |
| **Fixtures** | Custom Fields & Property Setters **only** | ⚠️ never ship doctype perms as fixtures (loses parent, wipes originals) |

**Desk Role Permission Manager** = experimentation only. In developer mode it writes back to the doctype
JSON, so capture there; otherwise the change is lost on the next image build. **Never the source of truth.**

---

## 7. How to add / change (procedures)

- **New custom doctype** → ask the one question (§4), paste the matching bucket block into its JSON
  `permissions[]`. Child table → leave `[]`.
- **New field that's sensitive** → set `"permlevel": 1` on the field; add one perm row at level 1 for the
  roles allowed to see/edit it.
- **Lock a stock doctype** (you don't own it) → add a row to `LOCKED_MATRIX` in `access/lockdown.py`
  listing *exactly* the roles allowed; the rebuild drops every other role incl. `All`.
- **New capability** (like WhatsApp) → create a role, gate the doctype's perms on it, keep it out of the
  Sales bucket roles.
- **Never** edit a stock doctype's JSON, and **never** add `All` to a business doctype.

---

## 8. The audit gate (how we verify, no guessing)

Two checks, run after seeding:

1. **The `All` audit (SQL).** Anything here that is **not** reference/own-records/essential is a violation:
   ```sql
   SELECT md.app_name, dp.parent, dp.`read`,dp.`write`,dp.`create`,dp.`delete`,dp.if_owner
   FROM `tabDocPerm` dp
   JOIN `tabDocType` dt ON dt.name=dp.parent
   JOIN `tabModule Def` md ON md.name=dt.module
   WHERE dp.role='All' AND dp.if_owner=0 AND (dp.`write`=1 OR dp.`create`=1 OR dp.`delete`=1);
   ```
2. **"Permitted Documents for User" report** — pick one Sales User + one Sales Manager, tick *Show
   Permissions*. A rep must see CRM, **not** `HD Ticket` or any bucket-3 settings.

> If either check surprises you, the doctype's `permissions[]` (or a `LOCKED_MATRIX` row) is wrong — fix
> the **code**, never the Desk.
