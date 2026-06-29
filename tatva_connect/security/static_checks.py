"""The static (no-bench) security gate — defined ONCE, here.

Single source of truth for WHAT the static gate runs: which tool, which args, which config. Two
consumers read this list and add no checks of their own, so they can never drift:

  * tatva_connect/tests/static/test_scanners.py — runs each as a pytest test (THE gate; CI runs this)
  * tatva_connect/security/tcsec.py             — runs each with a rich local UI (optional sugar)

The AST source-locks (no_sql_injection / no_perm_bypass / guest_endpoints) live beside the runner as
native pytest tests — they ARE the lock half of the lane, so they are not listed here.

Pure stdlib (no frappe, no pytest, no third-party) so it imports from any context — pytest, tcsec,
or a bare `python -c`.
"""
import os
import subprocess
import tomllib

APP = "tatva_connect"
SEMGREP_RULES = "tatva_connect/security/semgrep-rules"
SECRETS_BASELINE = "tatva_connect/security/.secrets.baseline"
# tests/ hold deliberate attack payloads + fake secrets; the baseline holds its own hashes. Both are
# excluded from the secret + pattern scanners (bandit already excludes tests/ via pyproject). App
# code is scanned in full.
SECRETS_EXCLUDE = r"(tatva_connect/tests/|^tatva_connect/security/\.secrets\.baseline$)"


def repo_root():
	"""The repo root = the dir holding pyproject.toml, found by walking up from this file (robust to
	where this package sits; identical in CI, locally, and the bench)."""
	d = os.path.dirname(os.path.abspath(__file__))
	while d != os.path.dirname(d):
		if os.path.exists(os.path.join(d, "pyproject.toml")):
			return d
		d = os.path.dirname(d)
	raise RuntimeError("repo root (pyproject.toml) not found above static_checks.py")


def _tracked(root):
	return subprocess.run(["git", "ls-files"], cwd=root, capture_output=True, text=True).stdout.split()


def _declared_deps_reqs(root, workdir):
	"""The app's declared runtime deps (pyproject [project.dependencies]) written to a requirements
	file for pip-audit — bench-free + reproducible (audits what is PINNED, not a live environment)."""
	with open(os.path.join(root, "pyproject.toml"), "rb") as fh:
		deps = tomllib.load(fh).get("project", {}).get("dependencies", [])
	path = os.path.join(workdir, "declared-deps.txt")
	with open(path, "w", encoding="utf-8") as fh:
		fh.write("\n".join(deps) + "\n")
	return path


def scanner_checks(root=None, *, tool=str, workdir):
	"""The no-bench scanner checks as ``[(name, argv), ...]`` — run from the repo root.

	`tool` resolves a tool name to an executable (default: the bare name, found on PATH; tcsec passes
	its venv resolver). `workdir` is a writable dir for transient inputs (the declared-deps reqs file).

	Posture notes (the ONE definition both consumers obey):
	  * lint = `ruff check` only. `ruff format --check` is intentionally NOT gated — the codebase
	    predates ruff-format; that is a separate deliberate mass-format pass, not a per-push gate.
	  * sast/secrets/semgrep exclude tests/ (deliberate payloads); app code is scanned in full.
	"""
	root = root or repo_root()
	reqs = _declared_deps_reqs(root, workdir)
	return [
		("lint", [tool("ruff"), "check", APP]),
		("sast", [tool("bandit"), "-r", APP, "-c", "pyproject.toml"]),
		("deps", [tool("pip-audit"), "-r", reqs]),
		(
			"secrets",
			[tool("detect-secrets-hook"), "--baseline", SECRETS_BASELINE, "--exclude-files", SECRETS_EXCLUDE, *_tracked(root)],
		),
		(
			"semgrep",
			[tool("semgrep"), "scan", "--error", "--metrics", "off", "--config", SEMGREP_RULES, "--config", "p/python", "--exclude", "tests", "--exclude", "security", APP],
		),
	]
