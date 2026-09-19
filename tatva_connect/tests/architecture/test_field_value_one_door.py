# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A surface migrated onto `lead.field_value` never decides a field's storage again — this fails the build if it does.

Each of these once held its own answer to "where does this value live", and each answer missed a kind:
Smart Views put multi-value and virtual fields into SQL, and the WhatsApp picker offered `"[]"` and raw keys.
The scope grows as surfaces migrate (docs/plans/2026-09-17-lead-field-value-foundation.md); it names the
MIGRATED surfaces, never the ones still waiting, so nothing here is an allowlist of known offenders.

AST-based: a docstring may name the rule it delegates to; only code is checked.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \
        --module tatva_connect.tests.architecture.test_field_value_one_door
"""
import ast
import pathlib

import frappe
from frappe.tests.utils import FrappeTestCase

APP = pathlib.Path(frappe.get_app_path("tatva_connect"))

# The surfaces that read a lead field only through `field_value`.
_MIGRATED = [*sorted((APP / "smartview").glob("*.py")), APP / "api" / "whatsapp.py"]

# Deciding storage by hand: reading selections, a column list or the virtual flag directly.
_FORBIDDEN_ATTRS = {
	"read_all": "read selections (ask field_value.read / page_selections)",
	"get_valid_columns": "decide what a column is (ask field_value.is_column / kind_of)",
	"is_virtual": "decide a field is computed (ask field_value.kind_of)",
}
_FORBIDDEN_NAMES = {"CRM Lead Multi Value"}


def _hits(path):
	where = path.relative_to(APP) if APP in path.parents else path.name
	out = []
	for node in ast.walk(ast.parse(path.read_text())):
		if isinstance(node, ast.Attribute) and node.attr in _FORBIDDEN_ATTRS:
			out.append(f"{where}:{node.lineno} .{node.attr} — {_FORBIDDEN_ATTRS[node.attr]}")
		elif isinstance(node, ast.Constant) and node.value in _FORBIDDEN_NAMES:
			out.append(f"{where}:{node.lineno} names {node.value!r} — read it through field_value")
	return out


class TestFieldValueOneDoor(FrappeTestCase):
	def test_no_migrated_surface_decides_storage_itself(self):
		hits = [h for path in _MIGRATED if path.name != "__init__.py" for h in _hits(path)]
		self.assertEqual(hits, [], f"a migrated surface decided a field's storage by hand: {hits}")

	def test_the_lock_catches_the_evasion_it_forbids(self):
		"""Proved by writing the evasion: a module reading selections directly must be caught."""
		probe = pathlib.Path(frappe.get_site_path("field_value_lock_probe.py"))
		probe.write_text("def f(doc, meta):\n\treturn multi_value.read_all(doc), meta.get_valid_columns()\n")
		try:
			found = [h.split(" .")[1].split(" ")[0] for h in _hits(probe)]
		finally:
			probe.unlink()
		self.assertEqual(sorted(found), ["get_valid_columns", "read_all"])

	def test_the_migrated_catalog_asks_the_kind(self):
		"""The lock is not satisfied by deleting the question: the Smart View catalog must CALL `kind_of`."""
		tree = ast.parse((APP / "smartview" / "catalog.py").read_text())
		calls = {n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
		self.assertIn("kind_of", calls)

