# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The bulk import is a CONSUMER of seams, and this fails the build if it ever grows its own.

Four things it must never do again, each of which existed somewhere in the app before this feature and
was consolidated rather than copied:

  the catalogue      -> `lead/mapping.py` answers which fields may be mapped, for every surface.
  a file             -> `tabular.py` reads and writes CSV and XLSX, for every direction.
  a bulk job         -> `partner_bulk_job.submit_job` inserts and enqueues, for every lane.
  a lead             -> `partner.bulk_creator` -> `_upsert_one`, for every source.

The checks are AST-based, not textual: a module that EXPLAINS the seam it delegates to must be able to
name it in a docstring without failing its own lock. A string in prose is not a call.
"""
import ast
import pathlib

import frappe
from frappe.tests.utils import FrappeTestCase

APP = pathlib.Path(frappe.get_app_path("tatva_connect"))
IMPORT_MODULE = APP / "lead_import"

# Reading one of these means the module resolved a lead's shape itself instead of asking the brain.
_BRAINS = {"CRM Lead API Field", "CRM Lead Section"}

# Writing a lead, a job or a file by hand — each has exactly one home, and none of them is here.
_FORBIDDEN_CALLS = {
	"get_meta": "resolve a field against a doctype (ask lead/mapping.py)",
	"read_csv_content": "parse a file (ask tabular.py)",
	"read_xlsx_file_from_attached_file": "parse a file (ask tabular.py)",
	"make_xlsx": "write a file (ask tabular.py)",
	"to_csv": "write a file (ask tabular.py)",
	"enqueue": "queue a job (ask partner_bulk_job.submit_job)",
	"_upsert_one": "write a lead (ask partner.bulk_creator)",
}

_FORBIDDEN_NEW_DOC = {"CRM Lead", "CRM Bulk Job"}


def _modules():
	return [p for p in IMPORT_MODULE.rglob("*.py") if p.name != "__init__.py"]


def _called_name(node):
	"""The bare callable name of a Call node — `a.b.c(x)` -> 'c', `f(x)` -> 'f'."""
	func = node.func
	if isinstance(func, ast.Attribute):
		return func.attr
	if isinstance(func, ast.Name):
		return func.id
	return None


class TestLeadImportHasNoSecondBrain(FrappeTestCase):
	def test_no_module_reaches_a_lead_brain_table_directly(self):
		hits = []
		for path in _modules():
			for node in ast.walk(ast.parse(path.read_text())):
				if isinstance(node, ast.Constant) and node.value in _BRAINS:
					hits.append(f"{path.relative_to(APP)}:{node.lineno} names {node.value}")
		self.assertEqual(hits, [], f"lead_import read a brain table directly: {hits}")

	def test_no_module_parses_writes_queues_or_upserts_by_hand(self):
		hits = []
		for path in _modules():
			for node in ast.walk(ast.parse(path.read_text())):
				if not isinstance(node, ast.Call):
					continue
				name = _called_name(node)
				if name in _FORBIDDEN_CALLS:
					hits.append(f"{path.relative_to(APP)}:{node.lineno} calls {name}() to "
					            f"{_FORBIDDEN_CALLS[name]}")
		self.assertEqual(hits, [], f"lead_import grew its own path: {hits}")

	def test_no_module_creates_a_lead_or_a_job_itself(self):
		hits = []
		for path in _modules():
			for node in ast.walk(ast.parse(path.read_text())):
				if not isinstance(node, ast.Call) or _called_name(node) != "new_doc":
					continue
				first = node.args[0] if node.args else None
				if isinstance(first, ast.Constant) and first.value in _FORBIDDEN_NEW_DOC:
					hits.append(f"{path.relative_to(APP)}:{node.lineno} new_doc({first.value!r})")
		self.assertEqual(hits, [], f"lead_import created a record that has one owner elsewhere: {hits}")

	def test_the_file_parser_has_exactly_one_home(self):
		"""App-wide: every reader and writer belongs to tabular.py, whoever the caller is."""
		hits = []
		for path in APP.rglob("*.py"):
			if path.name == "tabular.py" or "/tests/" in str(path) or "/.archive/" in str(path):
				continue
			for node in ast.walk(ast.parse(path.read_text())):
				if isinstance(node, ast.Call) and _called_name(node) in (
					"read_csv_content", "read_xlsx_file_from_attached_file", "make_xlsx", "to_csv",
					"build_csv_response", "build_xlsx_response",
				):
					hits.append(f"{path.relative_to(APP)}:{node.lineno}")
		self.assertEqual(hits, [], f"a file reader/writer outside tabular.py: {hits}")

	def test_a_bulk_job_has_exactly_one_creator(self):
		"""App-wide: submit_job is the only place a CRM Bulk Job is born, so no lane skips the caps."""
		hits = []
		for path in APP.rglob("*.py"):
			if path.name == "partner_bulk_job.py" or "/tests/" in str(path):
				continue
			for node in ast.walk(ast.parse(path.read_text())):
				if not isinstance(node, ast.Call) or _called_name(node) != "new_doc":
					continue
				first = node.args[0] if node.args else None
				if isinstance(first, ast.Constant) and first.value == "CRM Bulk Job":
					hits.append(f"{path.relative_to(APP)}:{node.lineno}")
		self.assertEqual(hits, [], f"a rival bulk-job creator: {hits}")
