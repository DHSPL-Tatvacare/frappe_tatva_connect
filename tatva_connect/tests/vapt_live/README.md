# `vapt_live/` — live VAPT emulation

> Part of the test tree — start at [`tests/README.md`](../README.md).

Fires the **32 VAPT Jun'26 findings** as real attacks over HTTP at a running deployment (UAT), as real
logged-in low-privilege users — the same thing an external pen-test does. `vectors.py` is the runnable
list; each vector is built from the endpoint's real signature and is **self-proving** (see below).

## What a run tells you

One legible line per finding:

```
[05] PASS  P2  Read all Contacts (get_data)  |  attacker=PermissionError control=HTTP200  |  denied by the permission layer
```

| Verdict | Meaning |
|---|---|
| **PASS** | attacker refused **by the permission layer**, and `admin` proved the endpoint really works |
| **FAIL** | attacker reached the data / the write landed → **finding STILL OPEN** |
| **ACCEPTED** | public-by-design (job board, programs, installed apps, schema) — reachable, nothing sensitive |
| **BROKEN** | the request crashed (500 / TypeError) or admin couldn't run it — **never a pass**; the payload is wrong |
| **SKIP** | no target of that type on the site |

**Why you can trust it:** every read fires twice — as the attacker (must be denied) and as `admin` (must
succeed). A crash can never be scored as a pass; it is flagged `BROKEN`. Login failures abort the whole
run (a refused login is indistinguishable from a blocked attack, so scoring it would be a lie).

---

## RUNBOOK — testing UAT

### Phase 0 — prerequisites (you / DevOps), once

1. **Deploy the fixes to UAT** — push `develop`, deploy, `bench migrate`, **restart workers** (N2/N6 are
   `override_whitelisted_methods` and only load at worker boot).
2. **Enable password login** on UAT temporarily (it is normally SSO-only). Confirm **2FA is OFF**.
3. **Check the API rate limit**: `sites/<site>/site_config.json` → `rate_limit`. If present, note
   `{limit, window}`; the runner paces 1 req / 2s by default (`--delay` to change).
4. **Three accounts** on UAT with known passwords:
   | persona | role | purpose |
   |---|---|---|
   | `admin` | System Manager | control (proves each payload works) + seeds/teardown |
   | `no_role` | No App Access | the strict-floor attacker |
   | `default_user` | LMS Student (or a real signup) | the LMS quiz/exercise attacker |

### Phase 1 — credentials (gitignored)

Create `tatva_connect/tests/vapt_live/.creds/uat.json` (this path is gitignored — verify with
`git check-ignore`):

```json
{
  "base": "https://one-uat.tatvacare.in",
  "host": "one-uat.tatvacare.in",
  "personas": {
    "admin":        {"email": "...", "password": "..."},
    "no_role":      {"email": "...", "password": "..."},
    "default_user": {"email": "...", "password": "..."},
    "guest":        {}
  }
}
```

The host guard **refuses to run against production** — the host must carry a non-prod marker (`uat`,
`staging`, `localhost`). `one.tatvacare.in` / `www.` are rejected.

### Phase 2 — run it

Run from the local docker bench (it has the package; HTTP goes out to UAT):

```bash
docker exec <backend-container> bash -lc \
  'cd /home/frappe/frappe-bench && bench --site dev.localhost execute tatva_connect.tests.vapt_live.vectors.run'
```

- `<backend-container>` is your local backend (e.g. `tatvalocal-backend-1`) — it only supplies the Python
  runtime; the attacks travel over HTTPS to the UAT URL in `.creds/uat.json`.
- Reads its target and logins from `.creds/uat.json`, logs each persona in ONCE (safe against a 3/60s
  lockout), paces 1 req / 2s, and prints the 32-line table + a tally.

### Phase 3 — read the result

- **`0 FAIL`** → every finding the report raised is closed on UAT. Sleep.
- **any `FAIL`** → that finding is still open on UAT — the line names the endpoint and shows the attacker
  got in. Do **not** sign off.
- **any `BROKEN`** → the run could not judge that vector (payload/target problem); fix before trusting.
- **`SKIP`** → no target of that type on UAT; seed dummy data of that doctype and re-run for full coverage.

### Phase 4 — after

- The run seeds a few throwaway records (a comment, a private file, an LMS quiz/exercise) as `admin` and
  **deletes them itself** in teardown. Nothing is left behind by design.
- **Disable password login on UAT again** and rotate the three test passwords.
- Delete `.creds/uat.json` (or leave it — it is gitignored and never leaves your machine).

---

## Files

| File | Role |
|---|---|
| `vectors.py` | the 32 findings as self-proving attack vectors + `run()` |
| `config.py` | reads the gitignored `.creds/uat.json`; **refuses production** |
| `attack.py` / `baseline.py` / `compare.py` | the older oracle-diff sweep (bench baseline → live diff); `vectors.py` is the simpler, report-faithful path and the one to use for a UAT rehearsal |

## The engine underneath (reused, not reinvented)

Login, session, and fail-loud safety live in `../authz/http_engine.py` — `login()` (password → sid
cookie), `whoami()` pre-flight, and hard aborts on 401 / 429 / CSRF. So the bench sweep and this live
runner share one transport and can never drift on "did the attack actually land".

## Known limits

- **Timer vector (16)** needs a real >quiz-duration wait; it SKIPs here and is proven by the bench test
  `tests/security/test_lms_assessment.py` instead.
- Coverage depends on target data existing on UAT — a doctype with no rows SKIPs. Seed dummy data for a
  complete 32/32 run.
