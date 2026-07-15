"""The static (no-bench) security gate, as pytest tests.

Runs the scanners defined ONCE in tatva_connect/tests/security/static_checks.py and asserts each is clean.
Together with the AST locks in this folder, ``pytest tatva_connect/tests/static`` IS the full static
gate — the same checks ``tcsec`` runs, minus the UI. No bench, no frappe.

Each scanner is its own parametrized test (id = lint / sast / deps / secrets / semgrep), so a failure
names exactly which tool found something.

This module is INTENTIONALLY inert without pytest: `bench run-tests` (the separate bench lane) imports
the app's test files, but the scanners need pytest + the tool CLIs (ruff/bandit/...) which a bench has
not. With no pytest, nothing here is defined — so the bench lane never trips on it. The AST locks in
this folder are unittest.TestCase and DO run in the bench too (pure AST, no tools needed).
"""
import subprocess
import tempfile

try:
	import pytest
except ImportError:  # a bench (no pytest) -> this no-bench lane is simply absent, nothing runs
	pytest = None


if pytest is not None:
	from tatva_connect.tests.security import static_checks

	_ROOT = static_checks.repo_root()
	_WORKDIR = tempfile.mkdtemp(prefix="tc-static-")
	_CHECKS = static_checks.scanner_checks(_ROOT, workdir=_WORKDIR)

	@pytest.mark.static
	@pytest.mark.parametrize("name,argv", _CHECKS, ids=[c[0] for c in _CHECKS])
	def test_scanner_clean(name, argv):
		proc = subprocess.run(argv, capture_output=True, text=True, cwd=_ROOT)
		assert proc.returncode == 0, (
			f"[{name}] exit {proc.returncode}\n--- stdout ---\n{proc.stdout[-4000:]}\n--- stderr ---\n{proc.stderr[-2000:]}"
		)
