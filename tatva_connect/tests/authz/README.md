# `authz/` — the permission / escalation engine

> Part of the test tree — start at [`tests/README.md`](../README.md).

> **One sentence:** **differential / metamorphic authz testing with mutation-based self-validation** — a data-driven framework that enumerates *who can act* × *how access could break*, judges each crossing against Frappe's own permission engine (never a hardcoded answer — assert `actual ⊆ native`, grain may narrow but never widen), and proves — by planting a known-bad per attack vector — that the suite can actually catch an escalation (`recall == 1.0` or the build fails). The differential/metamorphic half is §2; the mutation-based self-validation half is §9.

This document is the **philosophy and the map**. Read it before adding or running anything.

---

## 1. Why this exists

We layered a **grain entitlement** system `(vertical · group · program)` on top of Frappe's native permissions. The fear is simple and serious: *did our grain logic, somewhere, grant a user more than their role / user-permission / permlevel should allow?* That's a **privilege escalation** — a VAPT finding waiting to happen. This framework's job is to make that fear **measurable and repeatable after every change.**

---

## 2. The core idea — Frappe is the judge, not us

We **never hardcode** "user X should see 5 leads." We ask Frappe's own engine what's allowed and check our grain layer never exceeds it.

```
expected = what frappe.has_permission / get_list natively allows   ← the ORACLE
actual   = what our grain-gated path actually returns
PASS  iff  actual ⊆ expected        (grain may narrow, NEVER widen)
```

If a grain path ever returns *more* than the oracle → **🚨 escalation**. This is self-correcting: change the data, the oracle moves with it, the test stays honest.

> Grounded in Frappe v16 source (`permissions.py`, `db_query.py`, `document.py`): only **role perms** and **explicit DocShare** can ever *grant*; every other layer (`permission_query_conditions`, controller `has_permission` hooks, User Permissions, permlevel) can only *narrow*. So escalation is only possible where our code steps **outside** that composition — the custom field-render and the `ignore_permissions` writes. See vault `01-reference/access-control/00-how-access-resolves.md` §"engineering view".

> **Pick the right oracle per surface (audit 2026-06-26 — using the wrong one is a false negative):**
> `frappe.has_permission` is blind to BOTH `permission_query_conditions` (row scope on the 4 child doctypes) AND permlevel-1 field access (the grain fields). So: **row reads** → `oracle.native_visible_names` / `native_can_read_row` (get_list, runs PQC); **field/permlevel leaks** → `oracle.native_permitted_fields` (permlevel-aware); **write/create/delete on a row** → `oracle.native_would_allow` (doc mandatory); **deny-sweeps only** → `oracle.native_doctype_capability`.

---

## 3. Nothing is dropped for looking absurd

The combinations that look impossible are the **most important** tests.

> **"Guest user + permlevel-write on every doctype"** is not noise — it is a flagship catastrophe check. If it ever passes, that is a breach. Its expected outcome is **DENY**, and we assert it across **all** doctypes.

So a crossing is a test whether the expected outcome is *allow* or *deny*. We only ever exclude a crossing that is **logically impossible to even construct**, and even then it is **logged to `dropped.json` with a reason** — never silently removed.

---

## 4. The surface, curated into three tiers

### Tier 1 — Catastrophe sweeps (broad, programmatic, over ALL doctypes)
Blanket invariants that must hold for **every** doctype in the live DB. The generator enumerates `frappe.get_all("DocType")` and loops:

| Sweep | Expected on every doctype |
|---|---|
| Guest → read / write / create / delete / **permlevel-write** | **DENY** (write/create/delete/permlevel always; read only where intentionally public) |
| No-role authenticated user → write / create / delete | **DENY** |
| Each non-privileged role (Sales User, WhatsApp User, …) on doctypes outside its remit | **DENY** |
| Locked doctypes (`lockdown.py`) open to `All`/`Guest` write | **DENY** (drift guard) |

Cheap (each check is one `has_permission` call), exhaustive, and **every violation is listed by doctype name** in the report.

### Tier 2 — Targeted interaction cases (curated registry, subtle logic)
The grain × role × hierarchy contradictions on the **sensitive** doctypes (`CRM Lead`, `CRM Task`, `FCRM Note`, `CRM Call Log`, `WhatsApp Message`, `Contact`). These are the data-registry cases (§6).

### Tier 3 — Endpoint deny-sweep over REAL HTTP (the VAPT class, B1-B5)
The OWASP-API layer (Broken Object/Function/Property-Level Authorization). `cases.generate_http_cases()`
crosses the endpoint primitives (`registry/endpoints.py`) with the hostile principals to PRODUCE the
cases — never hand-listed. `test_endpoint_sweep.run` fires each as its persona via `http_engine`
(token auth) and judges the response against `oracle.native_http_verdict`: an endpoint that returns or
does MORE than native allows is an **escalation** (a live VAPT-class hole). `vapt/findings.py` is the
recall gate — every Jun'26 finding must be covered by a generated case. This tier runs in the
**committed HTTP lifecycle** (seed commit=True + teardown), not the in-process one.

The judge counts *rows*, never the metadata envelope (`_endpoint_allowed` reads `data`/`values`, not the
`columns`/`keys` wrapper). A live run may surface **known-benign residuals** that widen vs the oracle but
are not sensitive — the public File **folder skeleton** (`is_folder=1`, e.g. `Home/Attachments`) and a
role-scoped `get_apps` that returns only the caller's own entitled app. These are documented in
`vapt/findings.py` and the wire stays live, so a *real* private-File or cross-app leak still trips it.

**Two hostile users, on purpose:** `no_role` (truly zero roles — the strict floor; anything it reaches
is a definite bug) and `default_user` (only the platform-default `LMS Student`+`Wiki User` a real signup
gets — the faithful VAPT actor). A finding reachable by `no_role` is a code IDOR; reachable only by
`default_user` means the auto-strapped role is too broad (a platform decision). **Role fidelity is
enforced**: `generator` strips any app-auto-strapped role so each persona holds EXACTLY its declared set,
and `_assert_role_fidelity()` fails the seed on drift.

---

## 5. The dimensions (the "who/what") and attack vectors (the "how it breaks")

**Tuple dimensions** — a principal/context is one pick from each:

| Dim | Values |
|---|---|
| Grain held | none · one · multiple · wildcard-axis · ALL (System Mgr) |
| Grain source | Assignment Rule · Partner Mapping · `reports_to` roll-up · none |
| Hierarchy | leaf · manager-with-reports · no-hierarchy · deep-chain |
| Role(s) | Sales User · Sales Mgr · WhatsApp User · WhatsApp Admin · partner · no-role · System Mgr · Guest · **combos (role1+role2)** |
| User-permission fence | none · fenced · fenced + `ignore_user_permissions` field |
| Permlevel | field@0 · field@1 with access · field@1 **without** access |
| Doctype perms | locked (lockdown) · stock · drifted |
| Target row | in-grain · out-of-grain · **same-program/diff-vertical** · orphan-parent · wildcard |

**Attack vectors** — the direction of breakage each case probes:

| # | Attack vector | Plain meaning |
|---|---|---|
| A1 | Horizontal grain leak | see another grain's rows |
| A2 | Same-program / diff-vertical | grains #4 & #5 share program "Inside-Sales" — must NOT leak |
| A3 | Vertical escalation | role1 user gains role2's perms |
| A4 | Grain-vs-role contradiction | grain shows a field/row a role restriction should hide (who wins?) |
| A5 | Roll-up over-widening | manager inherits MORE than reports' union; cycle; depth bypass |
| A6 | Bypass-write escalation | an `ignore_permissions` path writes what native would deny |
| A7 | Field leak | out-of-grain field, too-wide universal floor, permlevel field exposed |
| A8 | Orphan / forged parent | child with missing/forged parent becomes visible |
| A9 | Switch-OFF exposure | a visibility switch OFF silently opens child doctypes |
| A10 | Native-method bypass | the 11 wrapped methods leak via the raw native call |
| A11 | Cross-app doctype leak | a role reaches another app's doctype |
| A12 | User-perm / DocShare over-grant | a fence or share grants more than intended |
| A13 | Partner-mapping abuse | disabled / multi-mapping / wrong-tenant attribution (invariant 16) |
| A14 | Public-intake guest abuse | anonymous web-form submit forces routing / grows a master / writes outside form scope |

A1-A14 are the GRAIN vectors (in-process, differential). The **endpoint layer** adds the OWASP-API
vectors below, derived from the Jun'26 VAPT and exercised by the generated HTTP sweep (Tier 3):

| # | Attack vector (OWASP API) | Plain meaning |
|---|---|---|
| B1 | IDOR object read (API1 BOLA) | supply an object id to a read method and read a record you can't natively read |
| B2 | IDOR object write (API1/API3) | `set_value` (or a write method) mutates another owner's record |
| B3 | Unauthorised function (API5 BFLA) | a no-access caller runs a list/create/delete it holds no capability for |
| B4 | Excessive data / info disclosure (API3) | a method leaks global/system info to a low-privilege caller |
| B5 | Private file access (API1 on File) | read a private File you can't natively read |

A1-A14 use a **relevance grid** (which attack applies to which tuple). B1-B5 need no grid: the endpoint
generator crosses every endpoint primitive with the hostile principals, and the oracle judges each.

---

## 6. Structure — data-driven registry, not a pile of files

```
tatva_connect/tests/authz/
├── README.md                ← you are here (philosophy + map)
│
│  ── REGISTRY (the curated surface, as DATA) ──
├── registry/
│   ├── dimensions.py       tuple dimensions + allowed values (who/what axes)
│   ├── attacks.py          the vectors: A1-A14 (grain) + B1-B5 (endpoint / OWASP API)
│   ├── endpoints.py        endpoint PRIMITIVES: GENERIC (doctype-parametric) + APP + SENSITIVE_DOCTYPES
│   └── cases.py            CaseSpec + generate_http_cases() (crosses endpoints × hostile principals)
│
│  ── HARNESS (machinery) ──
├── base.py                 AuthzTestCase: COMMS-OFF gate + rollback wiring (§7,§8)
├── comms.py                assert_comms_off() — the safety interlock (§7)
├── grains.py               the 5 canonical grains
├── roster.py               the dummy-user roster (incl. no_role AND default_user, §role-fidelity)
├── generator.py            seeds users (role-fidelity enforced) + grains/leads/tasks + tokens + creds.json
├── oracle.py               native_*() judges incl. native_http_verdict() — the judge (§2)
├── http_engine.py          token-auth HTTP transport: fire a real request AS any persona at any method
├── mutation.py             plants known-bad cases to measure False Negatives (§9)
├── confusion.py / matrix.py  scores TP/FP/FN/TN → confusion matrix (§9)
├── report.py               grid + metrics → console / JSON / feeds the PDF
├── vapt/findings.py        the Jun'26 VAPT findings as a coverage/recall map (endpoint_key + doctype)
│
│  ── TESTS (data-driven) ──
├── test_catastrophe_sweep.py   Tier-1 sweeps over ALL doctypes
├── test_registry_cases.py      Tier-2: parametrized over registry/cases.py (grain, in-process)
├── test_bypass_writes.py       the ignore_permissions paths (differential)
├── test_endpoint_sweep.py      Tier-3: the generated B1-B5 HTTP deny-sweep + VAPT coverage (§endpoint)
├── test_surface_audit.py       cross-app drift vs allowlist.json
├── test_stage_leakage.py       stage/sub-stage isolation across programs (grain)
├── test_self_validation.py     runs mutation.py → asserts the suite goes RED (no FN)
│
│  ── PLAYWRIGHT (break it through the real browser) ──
└── (moved) → tatva_connect/tests/authz_browser/
    ├── auth.setup.ts       reads creds.json → logs every persona in
    ├── leads.spec.ts       each grain user sees only in-grain leads/tasks
    ├── smartview.spec.ts   Smart View columns ⊆ grain∩role; no out-of-grain row data
    └── escalation.spec.ts  active attacks: URL-tamper to another grain's lead, forge
                            params, hit a hidden field, call a guarded method raw
```

> The browser specs now live in the security harness's LIVE lane
> (`tatva_connect/tests/authz_browser/`), alongside the load and partner-API pentest runners —
> one hood for everything that hits a running deployment. They still consume the same
> `authz_creds.json` this generator writes; only their folder changed.

**Each registry case has a stable ID** (e.g. `A2-grain4-lead-read-list`) so it is individually traceable in the report and the confusion matrix. Add a case = add data, not a file.

---

## 7. MANDATORY interlock — COMMS OFF or the test refuses to run

**Every test inherits `AuthzTestCase` and every test calls the comms-off gate first.** If WATI WhatsApp, Acefone telephony, or the follow-up automation are ON, the test **errors out immediately** — it does not run.

Why: tests create leads, tasks, and messages. If comms are live, a test could fire a **real WhatsApp message or phone call to a real number.** That is unacceptable. The gate is a safety interlock, not a nicety.

```python
# base.py (shape — exact switch keys resolved against live settings at build, never fabricated)
class AuthzTestCase(IntegrationTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        assert_comms_off()          # raises RuntimeError if any comms channel is enabled
        cls.addClassCleanup(frappe.db.rollback)

    def setUp(self):
        assert_comms_off()          # belt-and-suspenders: re-checked per test
        self.addCleanup(frappe.db.rollback)
```

`assert_comms_off()` checks the FIVE `CRM Tatva Automation` switches that gate a real send (audit-confirmed 2026-06-26): `WhatsApp::Channel::messaging`, `Telephony::Acefone::calls`, `Task::Assignment::followup`, and the two FCM push channels `Notify::Lead::assigned` + `Notify::Task::assigned` (assigning a lead or inserting a task with an assignee fires a real push to a rep's mobile — these are NOT covered by the first three). Per the constitution these default OFF; the gate enforces it and fails loud if anything flipped them on. The Playwright runner performs the **same** check before launching a browser.

---

## 8. Rollback — lean on Frappe, extend only where it can't reach

| Data | Cleanup mechanism |
|---|---|
| In-process tests (`has_permission`, `get_list`, `resolve_fields`) | **Frappe native:** `IntegrationTestCase` rolls back the DB per class; we add `self.addCleanup(frappe.db.rollback)` for per-**test** isolation. Zero custom teardown. |
| Committed data for Playwright / real HTTP | **Explicit, tagged teardown.** A browser/HTTP request runs in a *different* connection and cannot see an uncommitted transaction, so this data MUST be committed — which means rollback can't clean it. `generator.seed()` tags everything (`authz-test%`) and `generator.teardown()` deletes by tag, idempotently. |

This is the clean extension: native rollback everywhere it works; tagged teardown only for the committed Playwright fixtures, clearly fenced off.

---

## 9. The confusion matrix — proving the suite isn't blind

**A green run proves nothing on its own.** Green could mean "safe" or "the tests are asleep." So we validate the validator.

Ground truth per case comes from the **oracle** (native verdict) and **explicit fixture labels** (a seeded case is a *known* violation). The test predicts allow/deny. We score:

| | Violation truly exists | No violation |
|---|---|---|
| **Test flags it (red)** | ✅ **TP** — detection works | ⚠️ **FP** — false alarm, fix the test |
| **Test stays green** | ❌ **FN** — *the dangerous one*; suite is blind | ✅ **TN** — correct |

`mutation.py` deliberately introduces violations — seed an out-of-grain lead a user must not see; open a `lockdown` row; flip a known-good case to known-bad — and `test_self_validation.py` asserts the suite turns **red**. **A planted bug that stays green is a False Negative = a broken test, reported as a build failure.** That is the metric that makes this trustworthy for VAPT.

The **endpoint layer is mutated the same way** (`MUT-B1..B5`): each feeds the sweep's ONE escalation judge (`test_endpoint_sweep._escalates`, shared by the live run and the mutation) a known-bad synthetic HTTP response — a 200 that returns foreign data on a (doctype, action) the *real* oracle denies for `no_role` — plus a known-good control (403). It must flag the bad and not the good, so B1-B5 count toward recall exactly like A1-A14. Only the HTTP response is synthetic; the oracle verdict is genuine.

Metrics reported every run: counts of TP/FP/FN/TN, plus **precision** (TP/(TP+FP)) and **recall** (TP/(TP+FN)). Recall < 1.0 on the mutation set = **fail the build**.

---

## 10. Tools we use

**Frappe-native (machinery, ~90%):** `IntegrationTestCase` (rollback), `FrappeAPITestCase` (real HTTP), `frappe.set_user` / `set_user` (impersonate), `frappe.has_permission` + `frappe.get_list` (the oracle; `get_list` enforces perms, `get_all` bypasses — we never use `get_all` for an authz assertion), `frappe.permissions.{add_permission, update_permission_property, add_user_permission}`, `frappe.share.add`, `frappe.get_doc().insert()`.
**Ours (glue + scenarios, the test cases themselves are 100% ours):** the registry, generator, oracle wrapper, mutation, confusion scorer, report.
**Playwright:** drives the real CRM SPA at `http://dev.localhost:8000`; logs every persona in from `creds.json`; runs both visibility checks and active break attempts.

---

## 11. How to run + what you get

**In-process tiers (bench, rolled back per class):**
```bash
bench --site dev.localhost set-config allow_tests true
bench --site dev.localhost run-tests --module tatva_connect.tests.authz.test_registry_cases
```
**Tier-3 endpoint sweep (real HTTP; committed lifecycle, seeds + tears down):**
```bash
# runs IN the bench (needs frappe for oracle/seed); hits the site by its INTERNAL url.
# AUTHZ_HTTP_BASE defaults to the local gunicorn (http://localhost:8000); override per environment.
AUTHZ_HTTP_BASE=http://localhost:8000 \
  bench --site dev.localhost execute tatva_connect.tests.authz.test_endpoint_sweep.run
# prints: cases / escalations (live holes) / vapt_uncovered (must be 0). LOCAL-ONLY, not CI-gated.
```
**Playwright (after seeding + copying creds out of the bench):**
```bash
AUTHZ_CREDS=./authz_creds.json npx playwright test
```
**Unified via the CLI (one front door, existing UI/JSON/PDF/AI-explain):**
```bash
python security/tcsec.py authz            # runs sweeps + registry + bypass + surface + Playwright
python security/tcsec.py authz --pdf      # branded report incl. the confusion matrix
```

**Expected outcomes:**
- **Exit 0** only if: every catastrophe sweep DENIED, every registry case matched the oracle, the surface audit had no drift, **and recall == 1.0 on the mutation set** (the suite proved it can go red).
- **Exit non-zero** on any escalation (🚨), any drift, or any False Negative.
- **Artifacts:** `.custom-user-work/security/reports/authz-{report.json,confusion.json}` and the per-case grid (ALLOW / DENY / 🚨), each row tagged with its case ID.

---

## 12. Golden rule for extending this

Add a **case** (data in `registry/`) or a **sweep** (a loop in `test_catastrophe_sweep.py`) — not a bespoke test file. Every new case needs: a stable ID, a one-line English description, an expected verdict (and, if it's a known-bad, a mutation entry so it counts toward recall). If you can't state the English sentence and the expected verdict, the case isn't ready.
