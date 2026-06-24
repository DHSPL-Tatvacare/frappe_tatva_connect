# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The CI lock against SQL injection (OWASP A05:2025 Injection / WSTG-INPV-05).

Twin of `test_no_perm_bypass.py`, aimed at injection instead of authorization. It scans the
SOURCE (AST, not the running app) and FAILS if any line builds raw SQL by STRING INTERPOLATION
— the signature of an injection bug — into one of the raw-SQL sinks:

  * `frappe.db.sql(...)` / `frappe.db.sql_ddl(...)` / `frappe.db.multisql(...)`
  * `PseudoColumn(...)`        (pypika raw fragment — bypasses pypika's value escaping)

"Interpolation" = the first/query argument is an f-string, a `"...".format(...)` call, or a
`%` / `+` string concatenation. A plain string literal (optionally with `%(name)s` bind
params as a separate argument) is the SAFE form and is never flagged.

A flagged line is CLEARED only by an explicit `# sqli-ok: <reason>` marker on any line of the
call — the tiny, documented allowlist for the handful of spots that interpolate a CONSTANT
identifier (a table/column name fixed in code) while binding every value as a parameter. The
marker forces each deliberate raw fragment to be visible in one `grep` and justified in words.

OUT OF SCOPE (documented, like the perm-bypass lock skips smartview):
  * `tests/`   — payload corpora live here on purpose.
  * `patches/` — migration code, run by `bench migrate` as an operator; it takes NO HTTP
                 request input, so it is not part of the request-injection surface. (Its DDL
                 interpolates constant/loop identifiers over hardcoded column lists.)

Plant `frappe.db.sql("SELECT ... " + user_input)` in any in-scope file and this goes RED.
"""
import ast
import os
import subprocess

from frappe.tests.utils import FrappeTestCase

# The raw-SQL sinks: a string reaching any of these is executed verbatim (no escaping).
SQL_SINKS = ("sql", "sql_ddl", "multisql", "PseudoColumn")

# Per-line opt-out: the offending call carries this marker + a reason. Keep this list TINY.
OPT_OUT = "sqli-ok:"

_APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # .../tatva_connect
_SKIP = (os.sep + "tests" + os.sep, os.sep + "patches" + os.sep)


def _is_interpolation(node):
	"""True if `node` is a string built by interpolation (f-string / .format() / % / +) —
	the dangerous shape. A bare literal/Name/bind-param call is NOT interpolation."""
	if isinstance(node, ast.JoinedStr):  # f"...{x}..."
		return True
	if isinstance(node, ast.Call):  # "...".format(...)
		fn = node.func
		return isinstance(fn, ast.Attribute) and fn.attr == "format"
	if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Mod, ast.Add)):  # "..." % x  /  "..." + x
		return True
	return False


def _sink_name(call):
	"""The sink name for a Call, or None. Catches `frappe.db.sql`, bare `sql(`, `PseudoColumn(`."""
	fn = call.func
	if isinstance(fn, ast.Attribute):
		return fn.attr
	if isinstance(fn, ast.Name):
		return fn.id
	return None


def _violations_in(tree):
	"""Yield (sink, lineno, end_lineno) for every raw-SQL sink whose query argument is built by
	interpolation."""
	for node in ast.walk(tree):
		if not isinstance(node, ast.Call):
			continue
		name = _sink_name(node)
		if name not in SQL_SINKS:
			continue
		# The query string is the first positional arg (sql/sql_ddl/PseudoColumn all take it first).
		if not node.args:
			continue
		if _is_interpolation(node.args[0]):
			yield name, node.lineno, node.end_lineno


def _tracked_set():
	"""Absolute paths of git-tracked .py files, or None if git is unavailable. A CI lock polices
	the COMMITTED codebase, not untracked scratch scripts a dev bench may carry (CI runs on a
	clean checkout anyway) — so an unmarked interpolation in litter never makes this flap."""
	try:
		root = subprocess.run(
			["git", "-C", _APP_DIR, "rev-parse", "--show-toplevel"],
			capture_output=True, text=True, check=True,
		).stdout.strip()
		out = subprocess.run(
			["git", "-C", _APP_DIR, "ls-files", "*.py"],
			capture_output=True, text=True, check=True,
		).stdout
	except Exception:
		return None
	return {os.path.join(root, line) for line in out.splitlines() if line}


def _python_files():
	tracked = _tracked_set()
	for root, _dirs, files in os.walk(_APP_DIR):
		if "__pycache__" in root:
			continue
		if any(s in root + os.sep for s in _SKIP):
			continue
		for name in files:
			if not name.endswith(".py"):
				continue
			path = os.path.join(root, name)
			if tracked is not None and os.path.abspath(path) not in tracked:
				continue  # untracked bench litter — not part of the committed codebase
			yield path


def _scan():
	"""Return [(rel_path, sink, lineno), ...] — interpolated raw SQL with no `# sqli-ok:` marker."""
	violations = []
	for path in _python_files():
		with open(path, encoding="utf-8") as fh:
			lines = fh.read().splitlines()
		tree = ast.parse("\n".join(lines), filename=path)
		for sink, lineno, end_lineno in _violations_in(tree):
			span = "\n".join(lines[lineno - 1 : end_lineno])
			if OPT_OUT in span:
				continue
			violations.append((os.path.relpath(path, _APP_DIR), sink, lineno))
	return violations


class TestNoSqlInjection(FrappeTestCase):
	def test_no_interpolated_raw_sql(self):
		violations = _scan()
		if violations:
			report = "\n".join(
				f"  {path} — {sink}(...) built by string interpolation at line {lineno} "
				f"(use %(name)s bind params, or add a `# {OPT_OUT} <reason>` marker if the "
				f"interpolated part is a constant identifier)"
				for path, sink, lineno in violations
			)
			self.fail(f"{len(violations)} interpolated raw-SQL call(s):\n{report}")

	def _first_call_arg0(self, src):
		tree = ast.parse(src)
		return next(n for n in ast.walk(tree) if isinstance(n, ast.Call) and n.args).args[0]

	def test_detects_each_interpolation_form(self):
		# f-string, .format(), %-format, +concat -> all dangerous.
		self.assertTrue(_is_interpolation(self._first_call_arg0('frappe.db.sql(f"SELECT {x}")')))
		self.assertTrue(_is_interpolation(self._first_call_arg0('frappe.db.sql("SELECT {0}".format(x))')))
		self.assertTrue(_is_interpolation(self._first_call_arg0('frappe.db.sql("SELECT %s" % x)')))
		self.assertTrue(_is_interpolation(self._first_call_arg0('frappe.db.sql("SELECT " + x)')))

	def test_safe_forms_not_flagged(self):
		# Literal + bind params is the safe shape -> NOT interpolation.
		self.assertFalse(_is_interpolation(self._first_call_arg0('frappe.db.sql("SELECT %(p)s", {"p": x})')))
		self.assertFalse(_is_interpolation(self._first_call_arg0('frappe.db.sql("SELECT 1")')))

	def test_scanner_flags_a_planted_injection(self):
		# Self-check: an unmarked interpolated sink IS caught by the AST walk.
		tree = ast.parse('import frappe\ndef leak(u):\n    return frappe.db.sql("SELECT * FROM t WHERE x=" + u)\n')
		found = list(_violations_in(tree))
		self.assertEqual([v[0] for v in found], ["sql"])

	def test_marker_clears_a_constant_identifier(self):
		# The current tree must be clean: every real interpolation is a constant identifier + bind
		# params, marked `# sqli-ok:`. If this fails, a NEW unmarked interpolation was introduced.
		self.assertEqual(_scan(), [])
