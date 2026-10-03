# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A Task Form's Activity tab shows its question edits.

Run:
    bench --site dev.localhost run-tests --app tatva_connect --module tatva_connect.tests.activity.test_task_form_activity
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.api.change_history import change_history
from tatva_connect.tests.activity import task_type_fixture as ttf

SCHEMA = [{"fieldname": "zz_act_score", "label": "Act Score", "fieldtype": "Data"}]


class TestTaskFormActivity(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls.task_type = ttf.mint_type("ZZ Activity Probe", SCHEMA)

	def test_a_question_added_and_relabelled_reads_on_the_activity_tab(self):
		doc = frappe.get_doc("CRM Task Type", self.task_type)
		doc.append("schema", {"fieldname": "zz_act_note", "label": "Act Note", "fieldtype": "Data"})
		# Frappe writes no Version under tests unless asked (`ignore_version` defaults to `in_test`).
		doc.save(ignore_version=False)
		doc.schema[0].label = "Act Score Two"
		doc.append("schema", {"fieldname": "zz_act_break", "fieldtype": "Section Break"})
		doc.save(ignore_version=False)
		lines = [(c["label"], c["to"]) for row in change_history("CRM Task Type", doc.name) for c in row.get("changes") or []]
		self.assertIn(("Schema", "Act Note"), lines)
		self.assertIn(("Schema · Label", "Act Score Two"), lines)
		self.assertIn(("Schema", "Row 3"), lines)
		self.assertEqual(change_history("CRM Task Type", doc.name)[-1]["activity_type"], "creation")
