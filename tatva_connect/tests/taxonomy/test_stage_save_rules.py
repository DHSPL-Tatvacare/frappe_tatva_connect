# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Desk refuses a stage or picklist value that would quietly misbehave on leads, at the save that makes it."""
import frappe
from frappe.tests import IntegrationTestCase

PROGRAM_A, PROGRAM_B = "zz-stage-rules-a", "zz-stage-rules-b"


def _stage(program, stage, **extra):
	return frappe.get_doc({"doctype": "CRM Lead Stage", "program": program, "stage": stage, **extra}).insert()


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

	def test_a_stage_leaves_the_picker_with_its_first_substage_and_returns_with_its_last_gone(self):
		self.assertEqual(frappe.db.get_value("CRM Lead Stage", self.parent_a, "selectable"), 1)
		substage = _stage(PROGRAM_A, "Awaiting Document", substage_of=self.parent_a)
		self.assertEqual(frappe.db.get_value("CRM Lead Stage", self.parent_a, "selectable"), 0)
		self.assertIn("leaves the lead's Stage picker", frappe.get_message_log()[-1]["message"])
		substage.delete()
		self.assertEqual(frappe.db.get_value("CRM Lead Stage", self.parent_a, "selectable"), 1)
		self.assertIn("back in the lead's Stage picker", frappe.get_message_log()[-1]["message"])

	def test_a_second_sale_stage_in_one_program_is_refused(self):
		_stage(PROGRAM_B, "Enrolled", is_conversion_point=1)
		with self.assertRaisesRegex(frappe.ValidationError, "only one sale stage"):
			_stage(PROGRAM_B, "Paid", is_conversion_point=1)

	def test_a_misspelt_picklist_grain_is_refused(self):
		with self.assertRaises(frappe.LinkValidationError):
			frappe.get_doc({"doctype": "CRM Picklist Value", "category": "zz_rules", "value": "One",
			                "vertical": "zz-no-such-line"}).insert()

	def test_a_stage_merged_into_another_hands_it_its_substages_and_leaves_the_picker(self):
		old, target = _stage(PROGRAM_A, "Merge Old").name, _stage(PROGRAM_A, "Merge Target").name
		_stage(PROGRAM_A, "Merge Child", substage_of=old)
		frappe.rename_doc("CRM Lead Stage", old, target, merge=True)
		self.assertEqual(frappe.db.get_value("CRM Lead Stage", target, "selectable"), 0)
