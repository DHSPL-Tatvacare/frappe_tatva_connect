# hooks/ — local git hooks (enforcement moved off GitHub Actions)

These run the repo's security/quality gate **locally** so GitHub Actions costs nothing. Same checks,
same single source (`tatva_connect/security/static_checks.py` via `tcsec`), so local == the old CI.

## The split (speed-tiered)
| Hook | Runs | Cost | What |
|---|---|---|---|
| **pre-commit** | `tcsec locks` | ~instant | offline AST source-locks: SQLi guard, public-endpoint allowlist, no-perm-bypass |
| **pre-push** | `tcsec static` | seconds | the **full** offline gate: ruff + bandit + pip-audit + detect-secrets + semgrep + locks |

The heavy **bench/authz** suite (`tcsec runtime` / `dast`) needs the running devbench, so it is **not** a
hook — run it manually when you touch access/permission code.

## Enable (once per clone)
```bash
git config core.hooksPath hooks
```
Requires the security venv (the hook tells you if it's missing):
```bash
pyenv virtualenv 3.12.12 venv-python-frappe-sec && pyenv activate venv-python-frappe-sec
pip install -r tatva_connect/security/requirements.txt
```

## Notes
- Hooks are **fast local feedback + discipline**, not hard enforcement — `git commit/push --no-verify`
  bypasses them. Use bypass only in a genuine emergency.
- The GitHub workflow `.github/workflows/static.yml` is now **manual-only** (`workflow_dispatch`) — zero
  auto Actions cost. Trigger it by hand from the Actions tab if you ever want a clean-room CI run.
- One brain: both hooks call `tcsec`, which reads the same `static_checks.py` as the workflow — they cannot drift.
