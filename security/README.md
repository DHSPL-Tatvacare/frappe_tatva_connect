# `tcsec` — security + test harness

One flag-driven CLI that runs every static security scan **and** the runtime Frappe test suite
for this repo, from a single isolated pyenv venv. SAST (bandit) is the headline; the harness also
covers dependency CVEs, secret leakage (this repo is **public**), Frappe-aware semgrep rules, the
AST source-locks, the bench `run-tests` suite, and a gated sqlmap DAST lane.

## Install (once)

```bash
pyenv virtualenv 3.12.12 venv-python-frappe-sec
pyenv activate venv-python-frappe-sec
pip install -r security/requirements.txt
```

Run either with the venv activated (`tcsec` tools on PATH) or via the venv python directly:

```bash
python security/tcsec.py <command> [flags]
# or, fully-qualified without activating:
~/.pyenv/versions/venv-python-frappe-sec/bin/python security/tcsec.py <command>
```

## Commands

| Command          | Tool           | What it does                                              | Needs container |
|------------------|----------------|-----------------------------------------------------------|-----------------|
| `lint`           | ruff           | lint + format check (quality, not security)               | no              |
| `sast`           | bandit         | Python SAST over shipped app code                         | no              |
| `deps`           | pip-audit      | dependency CVE scan (declared deps; `--in-container` = bench env) | no / opt-in |
| `secrets`        | detect-secrets | secret-leak scan vs `.secrets.baseline`                   | no              |
| `semgrep`        | semgrep        | pattern SAST + custom Frappe rules (`security/semgrep-rules/`) | no          |
| `locks`          | pytest         | the decoupled AST source-locks (no bench)                 | no              |
| `runtime`        | bench          | `bench run-tests` via `docker exec` into the devbench     | **yes**         |
| `dast`           | sqlmap         | active SQLi probe of LIVE endpoints — **devbench only**   | **yes** (running app) |
| `static`         | —              | lint + sast + deps + secrets + semgrep + locks (all offline) | no           |
| `all`            | —              | static + runtime                                          | yes             |
| `secrets-baseline` | detect-secrets | (re)generate `security/.secrets.baseline`               | no              |

`lint` keeps ruff's `S` (flake8-bandit) rules **off** on purpose — bandit stays the single SAST
authority, so a finding is triaged in one tool, not two.

Exit code is non-zero if any step failed (CI-ready). Reports are written to
`custom-user-work/security/reports/` (gitignored); add `--format json` for machine-readable
artifacts (bandit.json, semgrep.json, pip-audit.json, locks-junit.xml).

## At-a-glance UI & analysis

Each step shows a spinner while running, then a color-coded `✓`/`✗` line with parsed stats
(bandit severity counts, semgrep findings, pytest pass/fail, …); findings print in an output
panel and a summary table closes the run. Flags:

- `--stream` — stream tool output live instead of the spinner (good for the long `runtime` lane).
- `--quiet` — stats only, suppress the findings panels.
Both `--ai-explain` and `--pdf` are **fully opt-in** and independent.

- `--ai-explain` — after the run, send **one clean, per-command prompt** to your local **`claude -p`**
  CLI (uses your existing Claude Code login — **no API key, no Anthropic SDK, no network call this
  harness manages**) and print a focused, project-aware analysis per tool. The `secrets` and `dast`
  raw outputs are **withheld** (they can contain secret values) — only their verdict is sent.
  `--claude-bin` overrides the CLI path. Requires Claude Code installed and logged in.
- `--pdf [PATH]` — a branded TatvaCare PDF report rendered with Playwright (banner + summary cards →
  per-command details). **Add `--ai-explain` to include the Claude analysis section**, which then
  leads the report (analysis first, then details). `--pdf` alone produces a findings-only report and
  makes **no** claude call. Default output `custom-user-work/security/reports/tcsec-report.pdf`.

  ```bash
  python security/tcsec.py static --pdf                    # findings-only PDF, no AI
  python security/tcsec.py static --pdf --ai-explain        # analysis-first PDF + console analysis
  python security/tcsec.py all --pdf ~/Desktop/sec.pdf --ai-explain
  ```

  Everything Playwright touches is gitignored: chromium installs to
  `custom-user-work/security/pdf/.browsers/` (via `PLAYWRIGHT_BROWSERS_PATH`), and the working
  HTML + PDF live under `custom-user-work/security/`. First `--pdf` run auto-installs chromium
  there (~120 MB). PDF generation is fail-open — a render error never changes the exit code.

## Common runs

```bash
python security/tcsec.py static                   # everything offline, in one shot
python security/tcsec.py static --ai-explain       # + per-command claude analysis
python security/tcsec.py all                       # + the bench runtime suite
python security/tcsec.py sast --app tatva_connect --app api-docs   # scan extra dirs
python security/tcsec.py deps --in-container        # audit the real bench env (frappe + all)
python security/tcsec.py static --format json --fail-fast
```

### Fully offline (no external calls at all)

`deps` queries the advisory DB and `semgrep`'s default `p/python` ruleset pulls from the registry;
for a zero-network run use the local lanes only:

```bash
python security/tcsec.py lint
python security/tcsec.py sast
python security/tcsec.py semgrep --semgrep-config security/semgrep-rules
python security/tcsec.py secrets
python security/tcsec.py locks
```

## Global flags

`--app` (repeatable, default `tatva_connect`) · `--container` (default `frappedev-frappe-1`) ·
`--site` (default `dev.localhost`) · `--bench-path` (default `/workspace/development/frappe-bench`) ·
`--format {txt,json}` · `--output-dir` · `--fail-fast` · `--stream` · `--quiet` · `--ai-explain` ·
`--pdf [PATH]` · `--claude-bin` · `--in-container` (deps) · `--semgrep-config` (semgrep, repeatable).

## DAST (sqlmap) — read before running

`dast` is an **active attack** lane (fires real injection payloads). It is **excluded from
`static`/`all`** and run deliberately. Safety:

- Requires `--target <url>`.
- **Refuses any host that isn't `localhost`/`dev.localhost`/`127.0.0.1`** unless you pass
  `--i-have-authorization`. **Never point it at production.**
- Needs a logged-in session: `--cookie "sid=<session-id>"`. Probe POST bodies with `--data`.
- `--level`/`--risk` default to `1` (low). Candidate endpoints live in `security/dast-targets.txt`.

```bash
python security/tcsec.py dast \
  --target "http://dev.localhost/api/method/tatva_connect.smartview.api.list_view?view=1" \
  --cookie "sid=<your-session-id>"
```

## How it relates to the existing tests

- The two AST source-locks (`test_no_sql_injection.py`, `test_no_perm_bypass.py`) were decoupled
  from `FrappeTestCase` via a fallback import, so they run **standalone** under `locks` **and**
  still run inside the bench under `runtime`. No behavior change in the bench.
- Bandit/semgrep `tests`, `.devbench`, `archive`, `node_modules`, `scripts` are excluded
  (`[tool.bandit]` in `pyproject.toml`) — test payload corpora and non-shipped trees, not attack surface.

## Bumping deps

Pinned in `security/requirements.txt` (public repo → reproducible CI). Bump deliberately, then
re-run `static` to confirm nothing regressed.
