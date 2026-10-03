# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A submitted task form records on its lead which activity was done and when, without editing the lead."""
import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import get_datetime, now_datetime

from tatva_connect.activity import api as activity_api
from tatva_connect.taxonomy import labels
from tatva_connect.taxonomy.labels import TASK_TYPE
from tatva_connect.tests.authz.grains import GRAINS, assert_masters_exist

TYPE_NAME = "ZZ Last Activity Probe"
ANSWER = "zz_outcome"
GRAIN = next(g for g in GRAINS if g["key"] == "Goodflip::India::Inside-Sales")


class TestASubmittedFormStampsItsLead(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		assert_masters_exist()
		cls.task_type = frappe.get_doc({
			"doctype": "CRM Task Type", "type_name": TYPE_NAME,
			"vertical": GRAIN["vertical"], "group": GRAIN["group"], "program": GRAIN["program"],
			"schema": [{"label": "ZZ Outcome", "fieldname": ANSWER, "fieldtype": "Data"}],
		}).insert(ignore_permissions=True).name

	def _lead(self):
		return frappe.get_doc({
			"doctype": "CRM Lead", "first_name": "ZZ Stamp Probe",
			"mobile_no": f"+91900000{int(frappe.generate_hash(length=8), 16) % 10000:04d}",
			"custom_vertical": GRAIN["vertical"], "custom_group": GRAIN["group"],
			"custom_current_program": GRAIN["program"],
		}).insert(ignore_permissions=True)

	def _assert_stamped(self, lead, before):
		name, at, modified = frappe.db.get_value(
			"CRM Lead", lead.name, ["custom_prospectactivityname_max", "custom_prospectactivitydate_max", "modified"])
		self.assertEqual(name, labels.label(self.task_type, TASK_TYPE))
		self.assertTrue(before <= get_datetime(at) <= now_datetime(), at)
		self.assertEqual(get_datetime(modified), get_datetime(lead.modified), "a stamp must not edit the lead")

	def test_a_new_punch_stamps_its_type_and_time_on_the_lead(self):
		lead, before = self._lead(), now_datetime()
		activity_api.save_activity(lead.name, self.task_type, {ANSWER: "Reached"})
		self._assert_stamped(lead, before)

	def test_completing_an_open_task_stamps_its_type_and_time_on_the_lead(self):
		lead = self._lead()
		task = frappe.get_doc({
			"doctype": "CRM Task", "title": TYPE_NAME, "custom_task_type": self.task_type, "status": "Todo",
			"reference_doctype": "CRM Lead", "reference_docname": lead.name,
		}).insert(ignore_permissions=True)
		before = now_datetime()
		activity_api.save_activity(lead.name, self.task_type, {ANSWER: "Reached"}, task=task.name)
		self._assert_stamped(lead, before)
