# `authz_browser/` — the browser half of `authz` (Playwright)

> Part of the test tree — start at [`tests/README.md`](../README.md).

Drives the real CRM SPA (`http://dev.localhost:8000`) to prove the grain
entitlement layer never leaks across grains, and that direct attacks (URL
tamper, raw API calls, permlevel-1 field reads) are denied. This is the browser
half of the `authz` framework — read `../README.md` (§6, §7, §11) first.

## SAFETY — the COMMS-OFF contract (non-negotiable)

These tests create/read leads. If WATI WhatsApp, Acefone telephony, the
follow-up automation, or either FCM push channel are ON, a test could fire a
**real message / call / push to a real number**. So the suite refuses to launch
a browser unless comms are proven off:

- `auth.setup.ts` asserts `process.env.AUTHZ_COMMS_OFF === "1"` and throws
  otherwise. No browser launches until that passes.
- The Python `tcsec` lane owns the actual check: it runs `assert_comms_off()`
  (the five `CRM Tatva Automation` switches — WATI messaging, Acefone calls,
  task-assignment follow-up, and the two FCM push channels) and **only then**
  exports `AUTHZ_COMMS_OFF=1` before invoking Playwright.
- Running Playwright directly without that env var is intentionally blocked. Do
  not set `AUTHZ_COMMS_OFF=1` by hand unless you have independently confirmed
  comms are off — that defeats the interlock.

## Credentials

The Python generator writes `authz_creds.json` into the bench site's private
files; the runner copies it out next to these specs (or point `AUTHZ_CREDS` at
it). Shape:

```json
[{ "persona": "grain_4", "email": "...", "password": "...", "grain_key": "Tatvapractice::India::Inside-Sales" }]
```

`grain_key` is `vertical::group::program`. `grain_4` and `grain_5` share the
program `Inside-Sales` — the A2 same-program / different-vertical trap the specs
target.

## Running

```bash
# Prereqs (the Python lane does this for you):
#   1. assert_comms_off() passed → export AUTHZ_COMMS_OFF=1
#   2. authz_creds.json copied out of the bench

npm install            # @playwright/test
npx playwright install # browsers (first run only)

AUTHZ_COMMS_OFF=1 AUTHZ_CREDS=./authz_creds.json npx playwright test
```

Env vars:

| Var | Purpose | Default |
|---|---|---|
| `AUTHZ_COMMS_OFF` | must be `"1"` or setup throws | unset (blocks) |
| `AUTHZ_CREDS` | path to creds JSON | `./authz_creds.json` |
| `AUTHZ_BASE_URL` | SPA base URL | `http://dev.localhost:8000` |

A spec may NOT import `auth.setup.ts` — Playwright refuses one test file importing
another ("test file X should not import test file auth.setup.ts") and the setup IS
a test file. The creds/persona helpers therefore live in `harness.ts`, a non-test
module; `auth.setup.ts` re-exports them so older imports still resolve, but new
specs import from `harness.ts`.

The `setup` project (`auth.setup.ts`) logs every persona in via the real login
form (`#login_email` / `#login_password`), falling back to
`POST /api/method/login`, and saves each session to `.auth/<persona>.json`. The
`authz` project depends on it and runs the specs with the right storageState.

## What each spec checks

- `leads.spec.ts` — each grain persona's `/crm/leads` shows no out-of-grain
  vertical (DOM + the list API the SPA uses).
- `smartview.spec.ts` — an authored Smart View stays within grain ∩ role: no
  out-of-grain row data, no permlevel-1 field as a column. **Selectors are
  TODO-anchored** and must be confirmed against the running SPA (cache-bust
  first, per UI constitution C.9); the assertions are final.
- `spotlight.spec.ts` — the spotlight's `status` (`disabled`/`building`/`ready`) is
  on every search response, no hit references a lead outside the persona's own
  list scope, and the same query renders "still being prepared" while building
  but "No results" on a ready index.
- `activity_fields.spec.ts` — Phase 4 of the task-slots plan: a declared activity
  field that used to live in the JSON payload is offered by the column picker AND
  by the Filter control, its cell text equals the value the fixture wrote, and
  filtering on it takes the grid from an exact 2 rows to an exact 1 — confirmed by
  an API re-read. Needs the `ZZ Phase 4 Browser View` fixture on the site.
- `escalation.spec.ts` — active attacks as grain_4 against a grain_5 lead:
  (a) URL tamper to the victim lead page, (b) `frappe.client.get` on the victim
  row, (c) permlevel-1 field read via `/api/resource`. All must be denied/empty.

## Gitignore

`.auth/` (session state) and `authz_creds.json` (credentials) MUST be
gitignored — they are secrets. `node_modules/` is already ignored repo-wide.
Add to the repo `.gitignore` if not already covered:

```
tatva_connect/tests/authz_browser/.auth/
tatva_connect/tests/authz_browser/authz_creds.json
```
