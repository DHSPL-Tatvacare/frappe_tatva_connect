# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A lead question takes its picker from the lead, so a Link one saves with no target of its own; a rep's Link question still must name one.

Run:
    bench --site dev.localhost run-tests --app tatva_connect --module tatva_connect.tests.activity.test_lead_link_questions
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.tests.activity import task_type_fixture as ttf


class TestLeadLinkQuestions(FrappeTestCase):
	def _new(self, name, row):
		return frappe.get_doc({
			"doctype": "CRM Task Type", "type_name": name, "vertical": ttf.VERTICAL, "group": ttf.GROUP, "schema": [row],
		})

	def test_a_lead_link_question_saves_with_no_target(self):
		ttf.mint_type("ZZ Lead Link Seed", [])
		doc = self._new("ZZ Lead Link", {"fieldname": "custom_condition", "label": "Condition", "fieldtype": "Link", "source": "Lead"})
		doc.insert()
		self.assertTrue(frappe.db.exists("CRM Task Type", doc.name))

	def test_a_rep_link_question_still_names_its_target(self):
		ttf.mint_type("ZZ Rep Link Seed", [])
		doc = self._new("ZZ Rep Link", {"fieldname": "zz_pick", "label": "Pick", "fieldtype": "Link", "source": "Activity"})
		self.assertRaises(frappe.ValidationError, doc.insert)
