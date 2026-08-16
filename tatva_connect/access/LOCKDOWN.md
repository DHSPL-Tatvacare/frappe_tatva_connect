# Lockdown — what was hardened, and why

Companion to `PERMISSIONS-POLICY.md`, which states the rules. This file is the record of what has been
**done** to this site's permission surface, one line per decision, so the next person can see the shape
without re-deriving it.

- `ledger.py` — the ONE declaration: which doctype is allowed to be what.
- `lockdown.py` — writes that declaration as Custom DocPerm, and pins field levels and app switches.
- This file — the ledger of work: what was closed, what was deliberately left open, and the cost accepted.

---

## 1. The layers, in order

A request passes all of these. Each answers a different question, and none substitutes for another.

| Layer | Question | Lives in |
|---|---|---|
| Role | may this person use the app at all | role assignment |
| DocPerm | may this role touch this doctype | `ledger.py` → `lockdown.apply()` |
| Row scope | which records of it | `permission_query_conditions`, `visibility.py`, `entitlement.py` |
| Field level | which fields of those | `lockdown.FIELD_LEVELS` + `_PERMLEVEL_1_FIELDS` |
| Endpoint | methods that bypass the engine | `native_guards.py` and the per-app overrides |

**Absence is a decision — where the inversion is armed.** A doctype not named in `ledger.OPEN` *resolves*
to `DENIED`, but `lockdown.apply()` only *writes* rows for what `rebuild_targets()` names: the ledger's own
doctypes, plus every doctype of an app armed in `ENFORCED_APPS`. With no app armed, an undeclared doctype
keeps the posture its app shipped, and the ledger's answer for it is advisory. Check `ENFORCED_APPS` before
relying on the inversion.

**What carries the rest.** Three controls sit below the matrix and do not depend on it:

| Control | Closes |
|---|---|
| `server_script_enabled = 0` in common site config | Server Script execution outright — `safe_exec` raises, whoever holds the DocPerm. Settable only in that file, never from the UI. |
| `developer_mode = 0` | schema authoring from the UI |
| roles disabled at go-live | the stock write grants on undeclared executable doctypes (Server Script, Web Page, Website Script, Report) — those grants belong to roles nobody holds |

So the executable surface is closed by configuration and by role assignment, not by this matrix. That is
deliberate, and it is why the disable list is load-bearing rather than tidy-up.

---

## 2. One shape per app

Every app resolves to the same three tiers. An app-level manager manages that app and nothing else;
platform administration is a single role across the whole site.

| App | Consumer | App manager | Platform |
|---|---|---|---|
| CRM | Sales User | Sales Manager | System Manager |
| Helpdesk | Agent | Agent Manager | System Manager |
| LMS | LMS Student | Course Creator + Batch Evaluator | System Manager |
| Wiki | Wiki User | Wiki Manager | System Manager |
| Insights | Insights User | Insights Admin | System Manager |
| WhatsApp | WhatsApp User | WhatsApp Admin | System Manager |

**Roles retired:** Wiki `Approver` — a review tier for a public docs site, which this is not.

**LMS `Moderator` is KEPT, with its teeth pulled.** It is lms's only test for "is this person staff"
(`has_moderator_role` reads the Has Role row directly; Course Creator and Batch Evaluator are consulted
nowhere in `can_modify_batch` / `can_modify_course`), so removing it removes LMS management itself. What it
must not carry is lms's `write`+`create` on `User`, which is a one-click route to System Manager — that
grant is stripped in `lockdown.BASELINE_ROLE_TRIMS`. Its five user-administration endpoints — which write
`Has Role` and delete `User` rows with `ignore_permissions` — are re-gated to the platform tier in
`native_guards.py`, so holding the staff flag no longer confers the right to hand it out.

---

## 3. What was closed, per app

| App | What was hardened |
|---|---|
| **CRM** | Reps work records but never delete them; managers curate master data but do not delete Link targets, which would orphan history. Integrations, credentials and automation config carry no sales grant. |
| **Helpdesk** | Agents keep delete, because the app's own endpoints are agent-callable and they are the operator there. Promotion to Agent Manager no longer carries a platform role with it. |
| **LMS** | Learners take courses; self-enrolment, self-certification, self-badging and review are closed. Marketplace doctypes (job board, payments, coupons, skills) are denied outright — this is internal training, not a storefront. Everything the Settings panel writes — site configuration, branding, the category taxonomy, badges, mail, gateways, and the user roster — is platform-only; a course manager authors content and nothing else. |
| **Wiki** | Everyone reads every handbook; only the wiki's manager writes. The change-request flow is how a page is saved here, not a review step, so it belongs to the writer alone. The superseded page flow and the repo-mirror doctypes are closed. |
| **Insights** | Consumers read the dashboards shared with them and author nothing. Teams grant *data*; sharing grants *dashboards*. The previous major version's doctypes are denied, which also closes its public-link surface. |
| **WhatsApp** | Capability doctypes gated by the app's own roles; account credentials are platform-only. |
| **Framework** | Comment editing and deletion are own-records-only. File's computed fields are readable by all and writable by none — hiding them would blank every attachment surface, and the write is the lock. |

---

## 4. Field-level locks

A field at permlevel 1 is invisible and unwritable to a role holding no permlevel-1 row. Used only where a
field is dangerous or secret, never for tidiness. Both halves are required: the field is reclassified via
Property Setter, and the level is granted to the roles that keep it.

| Doctype | Field class | Why |
|---|---|---|
| CRM Lead / CRM Deal | product-line fields | a rep must see the line to work; only a manager may move a record between lines |
| File | computed file attributes | marked read-only in the schema, which the framework does not enforce on save |
| Wiki Settings | page head markup, page script | rendered unescaped into every wiki page |
| Wiki Settings | repo-integration credentials | plaintext beside the settings a manager legitimately edits |
| LMS Batch | batch script, batch component | rendered to every learner in the batch |
| LMS Programming Exercise | test-case inputs and expected output | the answer key |
| LMS Program | member roster fields | names every colleague and their progress |
| Insights Data Source v3 | connection strings, service-account keys, headers | plaintext credentials; the password field is already encrypted, these are not |
| Insights Settings | permission switches, embed allowlist, data-store switch | each one unbinds analytics from the rest of this model |

**A child-table field resolves against its parent's permlevel access.** Grant on the parent, not the child.

**`if_owner` applies to a whole row.** "Read everyone's, edit only mine" cannot be expressed in one row, so
a few per-user doctypes are broader in the matrix than their intent. Documented rather than papered over.

---

## 5. Overrides, and why each exists

Every override either derives its answer from the permission engine, or gates a tier that no doctype
expresses. None of them restates the matrix — a second copy would drift.

| Module | Wired by | What it does |
|---|---|---|
| `native_guards.py` | whitelisted-method override | endpoints that read through the engine's back door get a permission check first, then the untouched native call |
| `helpdesk_roles.py` | whitelisted-method override | promotion within helpdesk stays within helpdesk |
| `native_guards._require_platform` | whitelisted-method override | lms's user administration asks for its staff flag, not an admin tier; these five ask for the platform tier as well |
| `insights_invitation.py` | doctype class | an invitation may only reach an address that already signs in here — the document hook catches every door, not just the button |
| `insights_publish.py` | doctype class | publishing a dashboard to the open internet is a platform act; withdrawing one is not |
| `insights_uploads.py` | whitelisted-method override | spreadsheet import is off, and every endpoint carrying it refuses, not just the first |
| `lms_permissions.py`, `lms_visibility.py`, `lms_member_guard.py` | query conditions, has_permission, doc events | course and batch content is scoped to the people enrolled in it |
| `contact_scope.py`, `picklist.py`, `visibility.py`, `entitlement.py` | query conditions | which records a person sees, by their assignment and their business line |
| `link_scheme.py`, `custom_field_guard.py`, `xss_guard.py` | doc events | schema and markup written at runtime cannot introduce a new reachable surface |
| `surfaces.py` | boot payload | which whole screens exist for a caller, answered once, from the same permissions |

**Rule:** a guard asks `frappe.has_permission`. It names a role only when the decision is *which tier*, and
there is no doctype that answers that. There is one spelling of "is this a platform administrator", and
everything that needs it delegates there.

---

## 6. Tail rights

Print, report, import and select are zero site-wide. Only three exceptions, each because a specific
product feature asks for that exact right:

| Right | Where | Why |
|---|---|---|
| `email` | the two sales record types | sending from the record |
| `export` | the sales record types | list export |
| `share` | the two analytics doctypes that own sharing | the app's own share dialog checks it |

`email` and `export` ride any role that already reads. `share` is granted per role, because a consumer
must never be able to re-share what was shared with them.

---

## 7. Costs accepted, and known limits

Recorded so they are not rediscovered as bugs.

- **Buttons the matrix does not gate.** Some upstream screens render a create or share button from the
  client without asking permission. Where our matrix is narrower than upstream's assumption, the button
  appears and the action refuses. Gating the button needs a fork of that app's frontend.
- **A published dashboard is outside this model by design.** The public path deliberately skips row and
  column permissions so an outside reader sees the page. Publishing is therefore restricted to the platform
  tier, and what a published page may contain is an editorial decision, not a permission one.
- **Analytics reads the site database directly.** Which means this ledger decides what any dashboard can
  show: no read on a doctype and its table resolves to nothing. That is the intended coupling.
- **A local analytical store, when enabled, writes a file to the host.** It is not covered by the file
  layer that offloads attachments. The switch that enables it is platform-only for that reason.
- **Upstream row-scoping quirks are not forked around.** Where an upstream permission query is looser than
  ours would be, it is documented and bounded by the layers above rather than patched in place.

---

## 8. The upgrade obligation — this is maintained, not set once

`ENFORCED_APPS` is empty **on purpose**, and it is the one place this model chooses judgement over
automation. Arming an app denies every doctype nobody has classified — including ones a persona needs
tomorrow — so a blind inversion does not make the platform safer, it makes it break in ways nobody
predicted and nobody can explain. A row here is worth something because a person decided it. Closing a
doctype whose purpose, roles and screens are unknown is not a decision; it is a guess wearing a lock.

The price of that choice is a standing duty, and it falls on whoever maintains this app:

**On every upgrade of frappe, crm, helpdesk, lms, wiki, insights or whatsapp — before it reaches an
environment people use — diff the doctypes and classify what is new.**

A new doctype arrives at whatever posture its author chose. Neither `apply()` nor `assert_locked` will
mention it: both read `rebuild_targets()`, which is this ledger plus armed apps, and a doctype in neither
is invisible to both. Nothing goes red. It simply exists, open at its author's default.

The tool already exists and never writes:

```
bench --site <site> console
>>> from tatva_connect.access import ledger_audit
>>> ledger_audit.report()              # counts, then the undeclared ranked by evidence
>>> ledger_audit.undeclared_ranked()   # the working list
```

It ranks by evidence rather than alphabet: a doctype carrying an executable `Code` field, one open to
`All`/`Guest` with nothing narrowing it at runtime, and one that actually holds rows, score highest. Work
that list from the top.

For each new doctype, one of three outcomes, and all three are written down:

| Outcome | What to do |
|---|---|
| A persona needs it | give it a bucket in `ledger.OPEN` |
| Nobody needs it | `"DENIED"` in `ledger.OPEN` — declared closed, not merely absent |
| Not understood yet | leave it undeclared and say so in the upgrade note; it stays at its app's posture until someone decides |

The third is honest and the first two are decisions. What is not acceptable is an upgrade landing with
nobody having looked.

Release cycles here are slow, so this is a small periodic task, not continuous work — but it is the task
that keeps everything above this line true.

---

## 9. How to verify

Do not read this file to check the current state — read the declaration, and resolve it.

1. Every doctype in `ledger.OPEN` resolves to real rows, and every name is a real doctype.
2. No bucket and no explicit row grants an unscoped write to everyone. `lockdown.assert_locked` fails the
   migrate if drift reopens one.
3. Every field named at permlevel 1 exists on its doctype, and its level is granted to at least one role.
4. Every override target imports and is callable.
5. A persona walk-through — one consumer and one manager per app, doing their normal day — is the only
   check that catches a surface nobody thought to declare.
