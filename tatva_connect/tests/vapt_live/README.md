# `vapt_live/` — live VAPT emulation

> Part of the test tree — start at [`tests/README.md`](../README.md).

Fires the **same attack cases** the `authz` engine generates, but over HTTP at a **running deployment**
(UAT), as real logged-in users. This is the dress rehearsal for what an external pen-test does.

**It reuses, never re-implements:** cases come from `authz/registry/`, personas from `authz/roster.py`,
transport from `authz/http_engine.py`, and the "did the endpoint do the thing?" judgment from
`authz/test_endpoint_sweep.py` — so the live verdict can never drift from the bench verdict.

## ⚠️ Current status — NOT usable against UAT yet

| Problem | Detail |
|---|---|
| **No password login** | `authz/http_engine.py` only sends `Authorization: token <api_key>:<api_secret>`. There is no `/api/method/login`, no session, no cookie. You cannot hand it a username + password. |
| **A failed login reads as a blocked attack** | The judge treats any non-2xx as "denied", so a `401` (never logged in) is indistinguishable from a `403` (logged in, correctly refused). **Wrong credentials produce a perfect green report.** |
| **Proven, not theoretical** | A local dogfood run reported `430 CORRECT / 0 escalations` while every single request had returned `401`. Nothing was actually tested. |

**Do not trust a green result from this folder until the two fixes below land.**

## Required fixes (scoped in `docs/plans/test-rejig/`)

1. **Password login** — `POST /api/method/login {usr, pwd}` → capture the `sid` cookie → send it on every
   request. Belongs in `authz/http_engine.py` so both the bench sweep and this folder gain it.
2. **Auth pre-flight** — after login, every persona calls `whoami` and must come back as the expected
   user. Any persona that fails → the run **aborts loudly**; its cases are marked `INVALID`, never
   `CORRECT`.

## The three layers

| Layer | File | Runs where | Job |
|---|---|---|---|
| 1 | `baseline.py` | **bench** | Ask the oracle what each persona *should* be allowed → export `expectations.json` |
| 2 | `attack.py` | anywhere | Fire the cases at the live URL, record a corpus JSONL. **No judging.** |
| 3 | `compare.py` | anywhere | Diff actual vs expected → `CORRECT` / `ESCALATION` / `OVER_BLOCK` |

The oracle needs a bench, so it cannot run remotely — layer 1 exports its **answer**, keyed by
`(persona, endpoint, doctype, target_relation)` rather than record ids, so it stays valid on a target
whose rows differ but whose roles match.

```bash
# 1. on the bench, once per code change
bench --site dev.localhost execute tatva_connect.tests.vapt_live.baseline.build

# 2 + 3. against the target in .creds/uat.json
python -m tatva_connect.tests.vapt_live.run
```

## Config and safety

`config.py` reads the **gitignored** `.creds/uat.json` (base URL, host, personas, admin token). It
**refuses to run against anything that looks like production** — a host must match a non-prod marker
or carry an explicit override. Active attacks are UAT-only, never prod.

## Known gaps beyond login

- **Baseline picks a weak target** — it takes any row per doctype, where the bench sweep uses a
  *tagged foreign-owned* one (e.g. a genuinely private File). Verdicts are softer than they should be.
- **Over-block artifacts** — `client.insert` fires `{"doc":{"doctype":X}}` (empty), so the live call
  fails *validation*, not permission. These need triaging into a declared known-benign list.
- **Phase A never built** — the disabled-account check (login, API token, password grant, OAuth,
  password reset must all refuse a disabled user) exists only as prose. It is not implemented anywhere.
