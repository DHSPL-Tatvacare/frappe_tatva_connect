# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Task Forms V2: a task is read with the form version it was answered on, and a removal something still uses cannot publish."""
import frappe
from frappe.tests import IntegrationTestCase

from tatva_connect.activity import api as activity_api
from tatva_connect.taxonomy.doctype.crm_task_type import crm_task_type as task_forms
from tatva_connect.tests.authz.grains import GRAINS, assert_masters_exist

KEPT, DROPPED = "zz_kept", "zz_dropped"
GRAIN = next(g for g in GRAINS if g["key"] == "Goodflip::India::Inside-Sales")


class TestAPublishedFormKeepsWhatItsTasksAnswered(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		assert_masters_exist()

	def _live_form(self, type_name):
		"""A form as reps meet it: published, then activated, through the builder's own verbs."""
		name = frappe.get_doc({
			"doctype": "CRM Task Type", "type_name": type_name, "is_logged_complete": 1,
			"vertical": GRAIN["vertical"], "group": GRAIN["group"], "program": GRAIN["program"],
			"schema": [
				{"label": "ZZ Kept", "fieldname": KEPT, "fieldtype": "Data"},
				{"label": "ZZ Dropped", "fieldname": DROPPED, "fieldtype": "Data"},
			],
		}).insert(ignore_permissions=True).name
		task_forms.publish(name)
		task_forms.activate(name)
		return name

	def _lead(self):
		return frappe.get_doc({
			"doctype": "CRM Lead", "first_name": "ZZ Versioned Probe",
			"mobile_no": f"+91900000{int(frappe.generate_hash(length=8), 16) % 10000:04d}",
			"custom_vertical": GRAIN["vertical"], "custom_group": GRAIN["group"],
			"custom_current_program": GRAIN["program"],
		}).insert(ignore_permissions=True)

	def _drop_in_a_draft(self, form):
		task_forms.revise(form)
		doc = frappe.get_doc("CRM Task Type", form)
		doc.set("schema", [row for row in doc.schema if row.fieldname != DROPPED])
		doc.save(ignore_permissions=True)

	def test_an_old_task_still_shows_an_answer_its_form_no_longer_asks(self):
		form = self._live_form("ZZ Versioned History Probe")
		task = activity_api.save_activity(self._lead().name, form, {KEPT: "kept", DROPPED: "old answer"})
		self._drop_in_a_draft(form)
		self.assertTrue(task_forms.publish(form)["ok"])
		self.assertEqual(activity_api.task_detail(task)["task"]["values"].get(DROPPED), "old answer")
		self.assertNotIn(DROPPED, {f.fieldname for f in activity_api.get_schema(form)}, "a new task is asked the new version")

	def test_a_removal_a_smart_view_still_uses_cannot_publish(self):
		form = self._live_form("ZZ Versioned Usage Probe")
		frappe.get_doc({
			"doctype": "CRM Smart View", "label": "ZZ Versioned View", "base_object": "Activity",
			"activity_type": form, "columns": frappe.as_json([f"activity:{DROPPED}"]),
		}).insert(ignore_permissions=True)
		self._drop_in_a_draft(form)
		answer = task_forms.publish(form)
		self.assertFalse(answer["ok"])
		self.assertIn("ZZ Versioned View", " ".join(p["message"] for p in answer["problems"]))
