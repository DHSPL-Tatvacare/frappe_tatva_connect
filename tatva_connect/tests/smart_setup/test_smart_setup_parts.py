# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The parts registry is whole, and a Check reads each part of a grain as it stands."""
import unittest

import frappe

from tatva_connect.smart_setup import parts, recipes
from tatva_connect.taxonomy import grain

PROGRAM = "zz-check-target"
STATUSES = {parts.DONE, parts.OFF, parts.MISSING, parts.NOT_SET_UP}


class TestSmartSetupParts(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")

	def tearDown(self):
		frappe.db.rollback()

	def test_every_part_names_a_real_doctype_and_says_how_it_lands(self):
		for part in parts.PARTS:
			with self.subTest(part=part.label):
				self.assertTrue(frappe.db.exists("DocType", part.doctype))
				if part.carried and part.doctype != "CRM Grain":
					self.assertIsNotNone(part.lands)
				if part.gather is not None:
					self.assertTrue(any(grain.columns(part.doctype, every=True)))

	def test_the_whole_grain_recipe_gathers_every_carried_part(self):
		self.assertEqual(set(recipes.get(recipes.WHOLE_GRAIN)["members"]), set(parts.gathered()))
		self.assertIs(recipes.get("Grain"), recipes.get(recipes.WHOLE_GRAIN))

	def test_a_check_reads_each_part_of_a_new_grain(self):
		frappe.get_doc({"doctype": "CRM Program", "program_name": PROGRAM}).insert()
		grain_row = frappe.get_doc({"doctype": "CRM Grain", "vertical": "Goodflip-Care", "group": "Zydus",
		                            "program": PROGRAM}).insert().name
		frappe.get_doc({"doctype": "Assignment Rule", "document_type": "CRM Lead", "description": "zz", "rule": "Round Robin",
		                "assigned_by_workflow": 1, "disabled": 1, "assignment_days": [{"day": "Monday"}],
		                "users": [{"user": "Administrator"}], "grain_vertical": "Goodflip-Care", "grain_group": "Zydus",
		                "grain_program": PROGRAM}).insert(set_name="zz check pool")
		found = {row["part"]: row for row in parts.check(grain_row)}
		self.assertEqual(list(found), [part.label for part in parts.PARTS])
		self.assertEqual(found[parts.label("CRM Grain")]["status"], parts.DONE)
		self.assertEqual(found[parts.label("CRM Lead Stage")]["status"], parts.MISSING)
		self.assertEqual(found[parts.label("Assignment Rule")]["status"], parts.OFF)
		self.assertTrue(found[parts.label("Assignment Rule")]["next_step"])
		self.assertTrue(all(row["status"] in STATUSES for row in found.values()))
