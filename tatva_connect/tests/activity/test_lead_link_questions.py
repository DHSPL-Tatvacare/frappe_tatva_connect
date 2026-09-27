# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A lead question takes its picker from the lead, so a Link one saves with no target of its own; a rep's Link question still must name one.

Run:
    bench --site dev.localhost run-tests --app tatva_connect --module tatva_connect.tests.activity.test_lead_link_questions
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.taxonomy import task_section_seed
from tatva_connect.taxonomy.doctype.crm_task_type.crm_task_type import builder_doc, list_target_columns
from tatva_connect.tests.activity import task_type_fixture as ttf


class TestLeadLinkQuestions(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		# The fixture commits, so it runs once here; each test's own inserts then roll back and a rerun starts clean.
		task_section_seed.ensure_rows()
		cls.probe = ttf.mint_type("ZZ Binding Probe", [])

	def _new(self, name, row):
		return frappe.get_doc({
			"doctype": "CRM Task Type", "type_name": name, "vertical": ttf.VERTICAL, "group": ttf.GROUP, "schema": [row],
		})

	def test_a_lead_link_question_saves_with_no_target(self):
		doc = self._new("ZZ Lead Link", {"fieldname": "custom_condition", "label": "Condition", "fieldtype": "Link", "source": "Lead"})
		doc.insert()
		self.assertTrue(frappe.db.exists("CRM Task Type", doc.name))

	def test_a_rep_link_question_still_names_its_target(self):
		doc = self._new("ZZ Rep Link", {"fieldname": "zz_pick", "label": "Pick", "fieldtype": "Link", "source": "Activity"})
		self.assertRaises(frappe.ValidationError, doc.insert)

	def test_the_builder_binds_where_desk_does(self):
		bindings = builder_doc(self.probe)["bindings"]
		self.assertEqual(frappe.db.get_value("CRM Task Section", bindings["lead_section"], "target_doctype"), "CRM Task Lead Snapshot")
		for home in bindings["activity"]:
			self.assertEqual(home["columns"], list_target_columns(home["section"]))

	def test_a_lead_question_saved_without_a_section_is_snapshotted_where_seeded_ones_are(self):
		doc = self._new("ZZ Desk Lead Row", {"fieldname": "first_name", "label": "First Name", "fieldtype": "Data", "source": "Lead"})
		doc.insert()
		self.assertEqual(doc.schema[0].section, frappe.db.get_value("CRM Task Section", {"is_lead_snapshot": 1}))

	def test_a_column_offers_only_the_answers_it_can_hold(self):
		engagement = {c["fieldname"]: c["takes"] for c in list_target_columns("engagement")}
		self.assertIn("Select", engagement["activity_status"])
		self.assertNotIn("Select", engagement["reschedule_at"])
		self.assertIn("Datetime", engagement["reschedule_at"])
