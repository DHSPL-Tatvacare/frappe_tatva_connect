# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A `@frappe.read_only()` ENDPOINT MAY NOT WRITE — an AST lock over the app.

`read_only()` swaps `frappe.local.db` to the replica for the whole call. A write underneath it does not
fail loudly on a replica — it fails on a connection that is not supposed to accept it, at the moment
something has ALREADY gone wrong, because the usual offender is `frappe.log_error()` in an except block.

WHAT THIS LOCKS, AND WHAT IT DOES NOT. This is a REGRESSION guard, not the trace. It reads the decorated
function's own module-level body: a bare `frappe.log_error(...)`, `frappe.db.set_value(...)`, `.save(...)`
or `.insert(...)` written directly inside it. It cannot see a write two calls deep — the plan's own
warning is that a shallow scan reads every partner-API `*_create` as pure, because the writes live in
`_base`. The call tree is traced by hand and recorded in `docs/plans/read-only-replica/`; this stops the
NEXT edit from adding a write to a file someone already cleared.

`frappe.write_only()(frappe.log_error)(...)` is the sanctioned shape and passes: it pins that one call
back to the primary. It is what `dashboard/api.py` uses and what `smartview/api.py` and `taxonomy/labels.py`
now use.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \\
        --module tatva_connect.tests.static.test_read_only_has_no_writes
"""
import ast
import pathlib
import unittest

APP = pathlib.Path(__file__).resolve().parents[2]

# A call that reaches the database to CHANGE something. `log_error` is here because an Error Log is an INSERT.
_WRITE_ATTRS = {"log_error", "save", "insert", "delete", "submit", "cancel", "set_value", "db_set", "commit"}


def _receiver_chain(node):
	"""The dotted receiver a call hangs off — `frappe.cache.set_value` -> {"frappe", "cache"}."""
	names = set()
	while isinstance(node, ast.Attribute):
		names.add(node.attr)
		node = node.value
	if isinstance(node, ast.Name):
		names.add(node.id)
	return names


def _is_read_only(fn):
	for d in fn.decorator_list:
		target = d.func if isinstance(d, ast.Call) else d
		if isinstance(target, ast.Attribute) and target.attr == "read_only":
			return True
	return False


def _write_calls(fn):
	"""Every write-shaped call written directly in this function, minus the ones pinned by write_only()."""
	pinned = set()
	for node in ast.walk(fn):
		# frappe.write_only()(frappe.log_error)(...) — the pinned call is the inner argument.
		if (isinstance(node, ast.Call) and isinstance(node.func, ast.Call)
				and isinstance(node.func.func, ast.Call)
				and isinstance(node.func.func.func, ast.Attribute)
				and node.func.func.func.attr == "write_only"):
			pinned.update(id(a) for a in node.func.args)
	found = []
	for node in ast.walk(fn):
		if not isinstance(node, ast.Call) or id(node.func) in pinned:
			continue
		f = node.func
		if not (isinstance(f, ast.Attribute) and f.attr in _WRITE_ATTRS):
			continue
		# `frappe.cache.set_value` is REDIS, not the database, and is safe on a replica connection.
		if "cache" in _receiver_chain(f.value):
			continue
		found.append(f"{f.attr}() at line {node.lineno}")
	return found


class TestReadOnlyHasNoWrites(unittest.TestCase):
	def test_no_decorated_endpoint_writes_in_its_own_body(self):
		offenders, checked = [], 0
		for path in APP.rglob("*.py"):
			if "/tests/" in str(path) or "/node_modules/" in str(path):
				continue
			try:
				tree = ast.parse(path.read_text(encoding="utf-8"))
			except SyntaxError:
				continue
			for node in ast.walk(tree):
				if isinstance(node, ast.FunctionDef) and _is_read_only(node):
					checked += 1
					for w in _write_calls(node):
						offenders.append(f"{path.relative_to(APP)}::{node.name} — {w}")
		self.assertTrue(checked, "no @frappe.read_only() endpoint found — the lock is watching nothing")
		self.assertEqual(
			offenders, [],
			"a read_only endpoint writes in its own body; wrap it in frappe.write_only()(...):\n  "
			+ "\n  ".join(offenders),
		)

	def test_the_lock_can_go_red(self):
		"""A companion that fails if the detector stops detecting."""
		src = (
			"import frappe\n"
			"@frappe.whitelist()\n"
			"@frappe.read_only()\n"
			"def f():\n"
			"    frappe.log_error(title='x')\n"
		)
		fn = ast.parse(src).body[1]
		self.assertTrue(_is_read_only(fn))
		self.assertEqual(len(_write_calls(fn)), 1)

	def test_a_redis_cache_write_is_not_an_offender(self):
		"""The cache is Redis; a replica connection never sees it. Locked so the exclusion is deliberate."""
		src = (
			"import frappe\n"
			"@frappe.read_only()\n"
			"def f():\n"
			"    frappe.cache.set_value('k', 1)\n"
			"    frappe.db.set_value('DT', 'n', 'f', 1)\n"
		)
		fn = ast.parse(src).body[1]
		self.assertEqual(len(_write_calls(fn)), 1, "the DB write must still be caught, the cache one must not")

	def test_a_pinned_log_is_not_an_offender(self):
		"""The sanctioned shape passes, or the lock would force people to delete their error logging."""
		src = (
			"import frappe\n"
			"@frappe.read_only()\n"
			"def f():\n"
			"    frappe.write_only()(frappe.log_error)(title='x')\n"
		)
		fn = ast.parse(src).body[1]
		self.assertEqual(_write_calls(fn), [])
