# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The workflows list opens on the columns an operator recognises a workflow BY.

The three grain axes were already being fetched for every row and simply never drawn, so an operator
scanning the list could not tell a Tatvapractice flow from a Goodflip one without opening it. They are
columns now, in containment order, and this is the lock on both halves of that:

  every declared key is a real field   - a mistyped key is an invalid column in the SELECT, so the whole
                                         page 500s for every user at once and nothing on the client can
                                         fall back
  vertical, group, program, in order   - containment order is the reading order; shuffled, three columns
                                         of similar-looking words stop being one fact read at a glance

Run:
    bench --site dev.localhost run-tests --module tatva_connect.workflow_engine.tests.test_workflow_list_columns
"""
import frappe
from frappe.tests.utils import FrappeTestCase

WORKFLOW_DT = "CRM Workflow"
STANDARD = {"name", "owner", "creation", "modified", "modified_by", "idx", "docstatus", "_assign", "_liked_by"}
GRAIN_ORDER = ["trigger_vertical", "trigger_group", "trigger_program"]


class TestWorkflowListColumns(FrappeTestCase):
	def setUp(self):
		self.declared = frappe.get_meta(WORKFLOW_DT).get_valid_columns()
		self.default = frappe.get_doc({"doctype": WORKFLOW_DT}).default_list_data()
		self.keys = [c["key"] for c in self.default["columns"]]

	def _known(self, fieldname):
		return fieldname in self.declared or fieldname in STANDARD

	def test_every_declared_key_is_a_real_field(self):
		for key in self.keys + self.default["rows"]:
			self.assertTrue(self._known(key), f"{key} is not a field on {WORKFLOW_DT}")

	def test_the_grain_reads_in_containment_order_and_as_one_block(self):
		found = [k for k in self.keys if k in GRAIN_ORDER]
		self.assertEqual(found, GRAIN_ORDER)
		# Adjacent, so the three read as one fact rather than three unrelated words down the row.
		first = self.keys.index(GRAIN_ORDER[0])
		self.assertEqual(self.keys[first : first + 3], GRAIN_ORDER)

	def test_the_declared_projection_actually_queries(self):
		frappe.get_all(WORKFLOW_DT, fields=self.default["rows"], limit=1)
