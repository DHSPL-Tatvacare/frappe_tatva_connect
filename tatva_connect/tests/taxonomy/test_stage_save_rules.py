# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Desk refuses a stage or picklist value that would quietly misbehave on leads, at the save that makes it."""
import frappe
from frappe.tests import IntegrationTestCase

PROGRAM_A, PROGRAM_B = "zz-stage-rules-a", "zz-stage-rules-b"


def _stage(program, stage, **extra):
	return frappe.get_doc({"doctype": "CRM Lead Stage", "program": program, "stage": stage, "selectable": 1, **extra}).insert()


class TestStageSaveRules(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		for program in (PROGRAM_A, PROGRAM_B):
			if not frappe.db.exists("CRM Program", program):
				frappe.get_doc({"doctype": "CRM Program", "program_name": program}).insert()
		cls.parent_a = _stage(PROGRAM_A, "In Program").name

	def test_a_parent_from_another_program_is_refused(self):
		with self.assertRaisesRegex(frappe.ValidationError, "Pick a parent stage from"):
			_stage(PROGRAM_B, "Waiting", substage_of=self.parent_a)

	def test_a_parent_stops_being_pickable_once_it_has_a_sub_stage(self):
		_stage(PROGRAM_A, "Awaiting Document", substage_of=self.parent_a)
		self.assertEqual(frappe.db.get_value("CRM Lead Stage", self.parent_a, "selectable"), 0)

	def test_a_second_sale_stage_in_one_program_is_refused(self):
		_stage(PROGRAM_B, "Enrolled", is_conversion_point=1)
		with self.assertRaisesRegex(frappe.ValidationError, "only one sale stage"):
			_stage(PROGRAM_B, "Paid", is_conversion_point=1)

	def test_a_misspelt_picklist_grain_is_refused(self):
		with self.assertRaises(frappe.LinkValidationError):
			frappe.get_doc({"doctype": "CRM Picklist Value", "category": "zz_rules", "value": "One",
			                "vertical": "zz-no-such-line"}).insert()
