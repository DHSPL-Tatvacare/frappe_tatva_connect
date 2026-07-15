# `tests/live` — the LIVE test lane (one hood)

Everything that hits a **running deployment** lives here — load, partner-API pentest, browser specs, and
the UAT authz pentest. The deterministic bench/static harness stays under `tatva_connect/tests/security/` (the
`tcsec` CLI) and `tatva_connect/tests/` (bench-discovered); this folder is its live sibling. **Nothing here
runs in CI** — it is manual, host-guarded (UAT only, never prod), and reads creds that CI does not have.

```
tests/live/
├── README.md      this runbook + folder map                         [tracked]
├── load/          LSQ→API load engine (+ uat.py live entrypoint)     [tracked]
├── pentest/       partner-API HTTP pentest (tuple-driven)            [tracked]
├── browser/       Playwright specs, headed + headless               [tracked]
├── .creds/        UAT logins · tokens · target URLs                  [IGNORED]
└── **/{data,reports,.auth,node_modules,test-results,.browsers}/      [IGNORED]
```

**Tracked vs ignored:** all runner *code* is tracked (public repo, no secrets). Anything naming a live
target, a credential, real/PHI data, or a run result is gitignored (see the `tests/live/**` block in
`.gitignore`). **One documented external dependency:** the load + partner runners read the operator's LSQ
creds and signed-off mapping from the migration bundle `docs/go-live/7-migrate-data/` — that is operator
data, referenced by a single constant, not duplicated here.

---

## UAT live pentest — runbook

**Goal:** prove the VAPT Jun'26 fixes actually *shipped to UAT*, not just that they pass on the dev bench.
The bench suite (`tests/authz/*`, `tests/security/test_vapt_authz.py`) already proves the fix in code. This
run proves the deployed UAT build enforces it.

**Authorized (2026-07-14):** full active test, **UAT only, never prod**. Transport = Playwright session +
raw HTTP. Write/delete tuples = **prove-and-stop** (attempt; if UAT wrongly allows, capture proof and
immediately reverse).

**Reuse, don't reinvent.** The tuple engine already exists: `tests/authz/roster.py` (personas),
`tests/authz/registry/{attacks,endpoints,cases}.py` (who × how × where), `tests/authz/http_engine.py`
(fire as a persona over HTTP), `tests/authz/vapt/findings.py` (the 24 findings as coverage ground truth),
`tests/live/browser/` (browser specs). The UAT runner only **re-points** these at the UAT base/host
with real captured sessions.

---

## Inputs required (you provide)

1. **UAT base URL** + the **Host header** the site answers to (e.g. `https://uat.tatvacare.in`, Host `uat...`).
2. **Login A** and **Login B** — two *enabled* accounts on **different grains** (different vertical/group/
   program) so a cross-account read is a genuine boundary crossing.
3. **The disabled account** — one login with `enabled=0` (may be a third account, or you tell me A/B which).
4. Auth surface facts: is login password-only, or is there SSO/OAuth? Do the accounts already have API
   key/secret, or do I mint tokens after you log them in?

All of this lands in a **gitignored** `tatva_connect/tests/live/.creds/uat.json` — never in the repo (mirrors how
`dast-targets.txt`, `loadtest/uat.py`, and `creds.json` are already ignored).

---

## Phase A — break the disabled account (before any enabled login)

A disabled Frappe user must be refused at **every** door. Each attempt expects **401/403**:

| # | Door | Attempt | Expect |
|---|---|---|---|
| A1 | Web login | Playwright: submit email+password at `/login` (headed, watch it) | denied, no session |
| A2 | API token | key/secret as `Authorization: token k:s` against `/api/method/frappe.auth.get_logged_user` | 401/403 |
| A3 | Password grant | `/api/method/login` with usr/pwd | fail, no `sid` |
| A4 | OAuth/SSO | if present, run the flow for the disabled user | denied |
| A5 | Password reset | `/api/method/frappe.core.doctype.user.user.reset_password` | no usable reset that yields a session |

Any success = **P1**. Stop and report.

## Phase B — horizontal break with A and B (both enabled)

You log in A and B in Chrome; I capture each `sid` (and mint an API token per account if allowed). Then I
run the VAPT tuple roster **live**, as A-attacking-B and B-attacking-A, over two transports:

- **Raw HTTP** (`http_engine`-style, session cookie / token): the full endpoint × doctype sweep — every
  finding in `vapt/findings.py` plus the generated catastrophe cases.
- **Playwright** (headed for the watch-list, headless for the full matrix): the browser-native crossings
  the raw sweep can't see (SPA flows, file upload → public-serve check).

### Tuple roster (each row = one live case; expected verdict for the attacker = DENY)

Derived from `vapt/findings.py` (24 findings) + the extra classes below. "Target" = the other account's row.

| Class | Vector | Endpoint(s) | Expect (attacker) |
|---|---|---|---|
| Contact read/delete (P1/P2) | `crm-get-data`, `client-delete` | `crm.api.doc.get_data`, `frappe.client.delete` | DENY / not-found |
| Comment IDOR (P1) | `client-set-value` on another's Comment | `frappe.client.set_value/delete` | DENY (if_owner) |
| Generic schema/enum (P2) | `getdoctype`, `validate_link`, `get_views`, `get_assigned_users` | framework | schema only, no rows |
| HD ticket/article (P2) | `hd-list-data`, `hd-ticket-contact`, `hd-ticket-activities`, `hd-article-stats` | helpdesk API | DENY (agent-only) |
| LMS drafts (P2/P3) | `lms-courses/batches/programs/job-details/job-opportunities` | lms API | published-only, no drafts |
| CRM lead/deal cross-grain (P2/P3) | `client-get`, `crm-get-data`, `linked-docs` | as A, target B's lead/deal | not-found |
| Call log read/modify (P3) | `client-get`, `client-set-value` on `CRM Call Log` | already overridden — re-verify | DENY |
| File private-blob BAC (P3) | reference another's private `file_url` on insert | File | DENY |
| **File upload → public serve** (our #8) | upload as attacker → GET the `/files/..` URL **unauthenticated** | storage | forced private; 403 unauth |
| Contact/Deal create (P3) | `client-insert` | Contact, CRM Deal | DENY or grain-forced |
| Installed-apps (P4) | `installed-apps` | framework | own-entitled only (accepted) |
| **Write/delete escalation** | A tries to `client.set_value`/`delete` B's lead/task | framework | not-found; **prove-and-stop** |

**More-than-VAPT extras** (adversarial, not in the report): SQLi payloads in list filters; `owner`/
`creation`/`name` injection on create (audit-field forgery); role-combo escalation (does holding two roles
leak either's perms); Guest/no-auth floor on every sensitive endpoint; horizontal by `mobile_no` (reach
B's lead by phone, not name).

### Prove-and-stop protocol (write/delete rows)

For every mutating tuple: fire it → if the response is a clean DENY/not-found, PASS. If UAT **allows** it
(200 + the mutation took), that is a live breach: capture the request/response as proof, then **immediately
reverse** (restore the prior value / undelete or recreate) and flag P1. Every created/changed record id is
written to the run report so nothing is left behind by accident.

### Headed vs headless

- **Headless** (`--headed=false`): the full matrix, both directions, fast. This is the scorecard.
- **Headed** (`--headed`): the watch-list only — Phase A disabled-login, one cross-account lead read, and
  the file-upload public-serve check — so you can see them happen.

---

## Corpus — every case logged, always (non-negotiable)

Every case that is *generated* is *recorded*, with its result — **there is no path where a tuple runs and
leaves no row, and no path where a tuple is skipped without a logged reason.** This mirrors the bench
harness's own rule (`recall == 1.0`, and a dropped case goes to `dropped.json` with a reason, never
silently removed). The UAT run produces a standing corpus we can point an auditor at.

**One row per (case × run), append-only, JSONL** — `tatva_connect/tests/live/reports/corpus-<env>-<ts>.jsonl`:

```
case_id          e.g. vapt-08-crm-get-data-contacts  (stable, ties back to vapt/findings.py)
run_id           one id per invocation (so re-runs stack, never overwrite)
timestamp        UTC
target           base + host (asserted UAT)
transport        http | playwright-headed | playwright-headless
attacker         persona/login used (A or B or disabled or guest)
victim           the row/account targeted
vector           class from the tuple roster
endpoint         dotted method or raw path
request          method, params (secrets redacted)
status_code      wire code
expected         DENY | not-found | published-only | schema-only | ...
verdict          PASS | FAIL | ERROR | SKIPPED
evidence         response snippet (ids scrubbed) proving the verdict
mutation         for prove-and-stop: what changed + reversal confirmation (or "none")
```

Rules:
- **Generated ⇒ logged.** The runner iterates the full roster; each case writes exactly one row before the
  next runs. A crash mid-case writes an `ERROR` row, not silence.
- **Skips are logged too.** A case not run (endpoint absent on UAT, auth surface missing) writes
  `verdict=SKIPPED` + reason — never dropped quietly.
- **Coverage assertion.** After the run, the corpus is checked against `vapt/findings.py`: every finding id
  must appear at least once, or the run is marked incomplete (same `uncovered()` contract as the bench).
- **Human report is derived, not primary.** The Markdown scorecard is generated *from* the JSONL, so the
  corpus is the source of truth and the report can never claim more than was logged.
- The JSONL + report live under the gitignored `tatva_connect/tests/live/reports/` (real targets + evidence never hit
  the public repo); ids are scrubbed so the corpus is safe to share as an audit artifact.

## Script inventory (built on your go-ahead)

Proposed home: `tatva_connect/tests/live/` at repo root (or `tests/authz/uat/`), **code tracked, creds+reports gitignored**.

1. `config.py` — reads `tatva_connect/tests/live/.creds/uat.json` (base, host, A, B, disabled, tokens). No secrets in code.
2. `session.py` — Playwright login for A/B, capture `sid` + storageState; mint API token if permitted.
3. `phase_a_disabled.py` — the 5 disabled-door attempts (A1–A5), PASS/FAIL table.
4. `phase_b_tuples.py` — imports `tests/authz/registry` + `vapt/findings`, runs the roster live over raw
   HTTP as A↔B, prove-and-stop cleanup, canary for recall.
4a. `corpus.py` — the append-only JSONL writer (one row per case×run) + the derived-report + coverage
    check. Every runner writes through this; nothing runs without a corpus row.
5. `playwright/uat.config.ts` + `disabled.spec.ts` + `cross-account.spec.ts` — extends the existing
   `tests/live/browser/` specs, headed + headless.
6. `run.py` — one entrypoint: `python -m tatva_connect.tests.live.run --phase a|b|all --headed` → PASS/FAIL + report.
7. `README.md` + this runbook — the checklist.

## Pre-flight checklist

- [ ] Target confirmed UAT (base + Host), **not** prod — asserted in code, refuses non-UAT host.
- [ ] `.creds/uat.json` present and gitignored; `git check-ignore` confirms.
- [ ] A and B are on different grains (cross-account = real boundary).
- [ ] Disabled account confirmed `enabled=0` on UAT.
- [ ] Only this run touches the bench/site (no concurrent load test — self-inflicted-contention rule).
- [ ] Report dir gitignored; ids scrubbed before anything leaves the machine.
- [ ] Corpus JSONL opened before the first case; coverage check green (every `vapt/findings.py` id logged).

## Run order

1. `run --phase a --headed` → disabled account must fail all 5 doors.
2. You log in A and B → I capture sessions.
3. `run --phase b` (headless, full matrix) → scorecard.
4. `run --phase b --headed` on the watch-list → you watch the key crossings.
5. Report: PASS/FAIL per tuple, any breach with proof + confirmation it was reversed.
