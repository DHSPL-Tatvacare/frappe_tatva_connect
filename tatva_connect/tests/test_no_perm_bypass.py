# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The CI lock for Leg 2 (the no-bypass rule).

Every `@frappe.whitelist()` method in `tatva_connect/` must authorize through the ONE
contract: a record-touching method asks `frappe.has_permission(...)`/`check_permission`,
an id-less list uses `get_list`, and a user-write never uses `ignore_permissions=True`
without a preceding gate. This test scans the source (AST, not the running app) and FAILS
if a whitelisted method uses a BYPASS pattern with no adjacent guard:

  * `frappe.get_all` / `frappe.db.get_all`   (skips permission_query_conditions)
  * `.insert/.save/.delete(ignore_permissions=True)`  (skips has_permission)

A method is CLEARED when its body contains one of the recognised GUARDS, OR the offending
line carries an explicit `# authz-ok: <reason>` opt-out marker (the tiny, documented
allowlist for partner-API internals / system webhooks / operator-only or self-scoped
writes). The marker forces every deliberate exception to be visible in one `grep`.

This test must PASS on the current tree (Batches 1-2 done); plant a bare `frappe.get_all`
in any whitelisted method with no guard/marker and it goes RED.
"""
import ast
import os

from frappe.tests.utils import FrappeTestCase

# Tokens that prove a function authorizes (any one in the function's CODE — decorators +
# executable statements, never docstrings/comments — clears its bypasses).
#   has_permission / check_permission  — Frappe's record gate (also crm's `ref_doc.has_permission`)
#   validate_access                    — crm.api.whatsapp's per-record parent read gate
#   only_for / _resolve_caller / _api  — partner-API role/caller gates (decorators)
#   _assert_access                     — near_me's territory-view access gate
#   _is_operator / PermissionError     — operator-only / throw-based owner gates (smartview writes)
#   get_list                           — the permission-aware list (the get_all replacement)
#   rate_limit                         — the public-lookup throttle marker
GUARD_TOKENS = (
	"has_permission",
	"check_permission",
	"validate_access",
	"only_for",
	"_resolve_caller",
	"_api",
	"_assert_access",
	"_is_operator",
	"PermissionError",
	"get_list",
	"rate_limit",
)

# Per-line opt-out: the offending line ends with this marker + a reason. Keep this list TINY.
OPT_OUT = "authz-ok:"

_APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # .../tatva_connect


def _is_whitelist(deco):
	"""True for `@frappe.whitelist` or `@frappe.whitelist(...)`."""
	node = deco.func if isinstance(deco, ast.Call) else deco
	return isinstance(node, ast.Attribute) and node.attr == "whitelist"


def _bypasses(func):
	"""Yield (label, lineno, end_lineno) for every bypass call inside `func`. The line span is
	the WHOLE call (a chained `frappe.get_doc({...}).insert(ignore_permissions=True)` starts on
	one line and ends many lines later), so an opt-out marker anywhere across it counts.

	Both call forms are caught: attribute-qualified (`frappe.get_all`) AND a bare name
	(`get_all(...)` from `from frappe import get_all`). For `ignore_permissions`, anything that
	is not a literal `False` (a `True` literal OR a variable/expression flag) is a bypass to
	guard or mark — a non-False value can always resolve True at runtime."""
	for sub in ast.walk(func):
		if not isinstance(sub, ast.Call):
			continue
		fn = sub.func
		name = fn.attr if isinstance(fn, ast.Attribute) else fn.id if isinstance(fn, ast.Name) else None
		if name == "get_all":  # frappe.get_all / frappe.db.get_all / bare get_all
			yield "get_all", sub.lineno, sub.end_lineno
		for kw in sub.keywords:
			if kw.arg != "ignore_permissions":
				continue
			is_false = isinstance(kw.value, ast.Constant) and kw.value.value is False
			if not is_false:
				yield "ignore_permissions", sub.lineno, sub.end_lineno


# Already-refactored, separately-audited layers with their OWN scope enforcement, out of
# this lock's scope: smartview (the grain surface — its composer ANDs the CRM Task/Lead
# permission_query_conditions into every list + self-scopes via or_filters on session.user).
# The partner API is NOT skipped — it clears natively via its @_api/_resolve_caller decorators.
_SKIP_DIRS = (os.sep + "smartview" + os.sep,)


def _python_files():
	for root, _dirs, files in os.walk(_APP_DIR):
		if "__pycache__" in root or os.sep + "tests" in root:
			continue
		if any(s in root + os.sep for s in _SKIP_DIRS):
			continue
		for name in files:
			if name.endswith(".py"):
				yield os.path.join(root, name)


def _guard_text(node):
	"""The function's CODE as text for the guard-token check — decorators + executable
	statements, with any leading docstring dropped. Built from the AST (via `ast.unparse`),
	so docstrings and `#` comments NEVER contribute a guard token: a function whose only
	mention of `get_list`/`has_permission` is in its docstring or a comment is NOT cleared."""
	parts = [ast.unparse(d) for d in node.decorator_list]
	stmts = node.body
	if (
		stmts
		and isinstance(stmts[0], ast.Expr)
		and isinstance(stmts[0].value, ast.Constant)
		and isinstance(stmts[0].value.value, str)
	):
		stmts = stmts[1:]  # drop the docstring
	parts.extend(ast.unparse(s) for s in stmts)
	return "\n".join(parts)


def _scan():
	"""Return [(path, func, label, lineno), ...] — unguarded, unmarked bypasses."""
	violations = []
	for path in _python_files():
		with open(path, encoding="utf-8") as fh:
			lines = fh.read().splitlines()
		tree = ast.parse("\n".join(lines), filename=path)
		for node in ast.walk(tree):
			if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
				continue
			if not any(_is_whitelist(d) for d in node.decorator_list):
				continue
			guarded = any(tok in _guard_text(node) for tok in GUARD_TOKENS)
			for label, lineno, end_lineno in _bypasses(node):
				span = "\n".join(lines[lineno - 1 : end_lineno])
				if OPT_OUT in span:
					continue
				if guarded:
					continue
				rel = os.path.relpath(path, _APP_DIR)
				violations.append((rel, node.name, label, lineno))
	return violations


class TestNoPermBypass(FrappeTestCase):
	def test_no_unguarded_bypass_in_whitelisted_methods(self):
		violations = _scan()
		if violations:
			report = "\n".join(
				f"  {path}::{func} — {label} at line {lineno} "
				f"(add a has_permission/get_list guard, or a `# {OPT_OUT} <reason>` marker)"
				for path, func, label, lineno in violations
			)
			self.fail(
				f"{len(violations)} whitelisted method(s) use a permission bypass with no "
				f"adjacent guard:\n{report}"
			)

	def _first_func(self, src):
		tree = ast.parse(src)
		return next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef))

	def test_scanner_flags_a_planted_bypass(self):
		# Self-check: an unguarded, unmarked get_all in a whitelisted method IS caught.
		func = self._first_func(
			"import frappe\n"
			"@frappe.whitelist()\n"
			"def leak():\n"
			"    return frappe.get_all('CRM Lead')\n"
		)
		self.assertTrue(any(_is_whitelist(d) for d in func.decorator_list))
		self.assertEqual([b[0] for b in _bypasses(func)], ["get_all"])

	def test_bypasses_catches_bare_name_and_non_false_flag(self):
		# T5: `from frappe import get_all` then a bare `get_all(...)` (no Attribute node).
		func = self._first_func(
			"from frappe import get_all\n"
			"@frappe.whitelist()\n"
			"def leak():\n"
			"    return get_all('CRM Lead')\n"
		)
		self.assertEqual([b[0] for b in _bypasses(func)], ["get_all"])
		# T6: `ignore_permissions=<var>` (non-literal) is a bypass; literal False is NOT.
		func = self._first_func(
			"@frappe.whitelist()\n"
			"def w(flag):\n"
			"    doc.save(ignore_permissions=flag)\n"
			"    other.save(ignore_permissions=False)\n"
		)
		self.assertEqual([b[0] for b in _bypasses(func)], ["ignore_permissions"])

	def test_docstring_or_comment_token_does_not_clear(self):
		# T2/T3: a guard token appearing ONLY in a docstring or a comment must NOT clear a real
		# unguarded bypass — _guard_text reads code, not prose.
		func = self._first_func(
			"@frappe.whitelist()\n"
			"def leak():\n"
			'    "uses has_permission and get_list semantics"  # get_list note\n'
			"    return frappe.get_all('CRM Lead')\n"
		)
		self.assertFalse(any(tok in _guard_text(func) for tok in GUARD_TOKENS))

	def test_decorator_token_clears(self):
		# A guard carried by a decorator (e.g. partner-API `@_api`) clears the function natively,
		# with no in-body token and no marker.
		func = self._first_func(
			"@frappe.whitelist()\n"
			"@_api\n"
			"def lead_list(**kwargs):\n"
			"    return frappe.get_all('CRM Lead')\n"
		)
		self.assertTrue(any(tok in _guard_text(func) for tok in GUARD_TOKENS))
