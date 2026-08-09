# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The runs list's default columns — the declaration `crm.api.doc.get_data` reads before a reader has a saved view.

It is one static dict, and that is exactly why it needs a lock: a mistyped key is not a wrong label, it
is an invalid column in the SELECT, so the whole page 500s for every user at once and nothing on the
client can fall back. Two contracts, both cheap:

  every declared key is a real field                 - so the query the list runs is legal
  `subject_doctype` is fetched though never drawn    - the Lead column is a Dynamic Link, and a Dynamic
                                                       Link's target is read off the ROW; drop it and
                                                       every lead cell silently prints a docname

Run:
    bench --site dev.localhost run-tests --module tatva_connect.workflow_engine.tests.test_runs_list_defaults
"""
import frappe
from frappe.tests.utils import FrappeTestCase

JOURNEY_DT = "CRM Workflow Journey"

# frappe's own columns, present on every table and never in the doctype's field list.
STANDARD = {"name", "owner", "creation", "modified", "modified_by", "idx", "docstatus", "_assign", "_liked_by"}


class TestRunsListDefaults(FrappeTestCase):
	def setUp(self):
		self.declared = frappe.get_meta(JOURNEY_DT).get_valid_columns()
		self.default = frappe.get_doc({"doctype": JOURNEY_DT}).default_list_data()

	def _known(self, fieldname):
		return fieldname in self.declared or fieldname in STANDARD

	def test_every_default_column_is_a_real_field(self):
		for column in self.default["columns"]:
			self.assertTrue(self._known(column["key"]), f"column {column['key']} is not a field on {JOURNEY_DT}")

	def test_every_fetched_row_is_a_real_field(self):
		for row in self.default["rows"]:
			self.assertTrue(self._known(row), f"row {row} is not a field on {JOURNEY_DT}")

	def test_the_lead_column_can_resolve_its_target(self):
		# The column is a Dynamic Link, so its target doctype is the value of another field ON THE ROW.
		lead = next(c for c in self.default["columns"] if c["key"] == "subject_name")
		self.assertEqual(lead["type"], "Dynamic Link")
		self.assertEqual(lead["options"], "subject_doctype")
		self.assertIn("subject_doctype", self.default["rows"])

	def test_the_declared_projection_actually_queries(self):
		# The one thing neither check above can prove: that frappe accepts this exact field list.
		frappe.get_all(JOURNEY_DT, fields=self.default["rows"], limit=1)
