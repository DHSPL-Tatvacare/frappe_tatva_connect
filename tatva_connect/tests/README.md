# `tests/` — the one map

Every test in this app lives here. **This file is authoritative**: each folder below links to its own
README for detail, and no folder-level doc may contradict this page.

One question decides how a test runs: **does it need a database?**

| | Runner | Needs a site/DB |
|---|---|---|
| Static checks | `pytest` | No — plain Python |
| Everything else | `bench run-tests` | Yes — `FrappeTestCase` wraps each test in a transaction and rolls it back |

---

## The folders

| Folder | What it is | How to run | Bucket |
|---|---|---|---|
| [`static/`](static) | Scanners + AST source-locks. No bench, no site. | `pytest tatva_connect/tests/static` | **Auto** |
| [`security/`](security/README.md) | `tcsec` — the one scanner CLI (lint, SAST, deps, secrets, semgrep) + security regressions | `tcsec static` · `tcsec runtime` | Auto + manual |
| [`authz/`](authz/README.md) | The permission/escalation engine. Generates ~484 attack cases and judges each against Frappe's own engine. | `bench --site <site> execute tatva_connect.tests.authz.test_endpoint_sweep.gate` | **Gated** |
| [`authz_browser/`](authz_browser/README.md) | The browser half of `authz` — Playwright. Catches what HTTP can't: URL tampering, SPA flows, permlevel field reads. | see its README | **Gated** |
| [`vapt_live/`](vapt_live/README.md) | Live VAPT emulation against a running target (UAT). Fires the same registry cases over HTTP and diffs against the oracle's expectation. | `python -m tatva_connect.tests.vapt_live.run` | **Manual** |
| [`partner_api_load/`](partner_api_load/README.md) | Volume test — drives real LeadSquared data through the partner API and times it. **Holds real patient records.** | see its README | **Manual, local only** |
| [`partner_api_parity/`](partner_api_parity/README.md) | Migration data-fidelity — proves the partner API ingests the same data the migration writes. **Not a security test.** | see its README | **Manual** |
| `api/` `lead/` `storage/` `automation/` … | Ordinary feature tests | `bench run-tests --module …` | Manual |

---

## The three buckets

| Bucket | Rule | Members |
|---|---|---|
| **Auto — offline** | Runs on every commit/push via `hooks/`. Never needs a bench. | `static/`, the `tcsec static` scanners |
| **Gated — needs a site, must not be optional** | Can't be a git hook (needs a bench), but forgetting it ships a silent escalation | `authz/`, `authz_browser/`, the bench suite |
| **Manual — judgment** | Needs credentials, hits a live target, or handles real data | `vapt_live/`, `partner_api_load/`, `partner_api_parity/` |

**CI does not run any of this by default.** `.github/workflows/static.yml` is manual-only
(`workflow_dispatch`); enforcement lives in `hooks/pre-commit` (fast locks) and `hooks/pre-push`
(full offline gate). That is deliberate — `partner_api_load/` handles real patient data and
`authz_browser/` can trigger real messages, so neither may ever run unattended.

---

## Running tests day to day

```bash
# fast, no bench
pytest tatva_connect/tests/static

# one module / one test / one class
bench --site dev.localhost run-tests --module tatva_connect.tests.lead.test_x
bench --site dev.localhost run-tests --module <mod> --test test_my_thing
bench --site dev.localhost run-tests --case TestMyClass

# the whole app
bench --site dev.localhost run-tests --app tatva_connect
```

In docker, wrap with:
`docker exec <backend-container> bash -lc 'cd /home/frappe/frappe-bench && bench …'`

---

## Secrets and real data — non-negotiable

`.creds/`, `data/`, `reports/`, `.auth/`, `node_modules/` under `tests/**` are gitignored
**path-agnostically**, so they stay ignored no matter how this tree is reorganised. This repo is
public and `partner_api_load/data/` holds real patient records. Never relax those rules.
