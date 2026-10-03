# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A submitted task form stamps its type and time on its lead in an ordinary lead save, from a rep or the Partner API alike."""
import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import get_datetime, now_datetime

from tatva_connect.activity import api as activity_api
from tatva_connect.taxonomy import labels
from tatva_connect.taxonomy.labels import TASK_TYPE
from tatva_connect.tests.activity import partner_fixture
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
		cls.partner = partner_fixture.make_partner("zz-stamp-partner@example.invalid", GRAIN)

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
		self.assertGreater(get_datetime(modified), get_datetime(lead.modified), "the stamp is an ordinary lead save")

	def test_a_rep_s_submitted_form_stamps_its_type_and_time_on_the_lead(self):
		lead, before = self._lead(), now_datetime()
		activity_api.save_activity(lead.name, self.task_type, {ANSWER: "Reached"})
		self._assert_stamped(lead, before)

	def test_a_partner_api_activity_stamps_the_lead_as_a_rep_s_does(self):
		lead, before = self._lead(), now_datetime()
		partner_fixture.create_activity(self.partner, lead.name, TYPE_NAME, {ANSWER: "Reached"})
		self._assert_stamped(lead, before)
