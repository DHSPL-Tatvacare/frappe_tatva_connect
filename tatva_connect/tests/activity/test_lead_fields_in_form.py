# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A lead field asked inside an activity form opens prefilled, keeps its value on the task, and a changed answer goes back to the lead."""
import frappe
from frappe.tests import IntegrationTestCase

from tatva_connect.activity import api as activity_api
from tatva_connect.lead import detail as lead_detail
from tatva_connect.tests.activity import partner_fixture
from tatva_connect.tests.authz.grains import GRAINS, assert_masters_exist
from tatva_connect.tests.automation import field_allowlist

LEAD_FIELD = "job_title"
ACTIVITY_FIELD = "zz_lf_note"
PREFILLED = "ZZ already on the lead"
CHANGED = "ZZ what the rep learnt on the call"
LATER = "ZZ corrected on the lead later"
GRAIN = next(g for g in GRAINS if g["key"] == "Goodflip::India::Inside-Sales")


class TestLeadFieldsInForm(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		assert_masters_exist()
		cls.task_type = cls._task_type("ZZ Lead Fields Probe")
		field_allowlist.seed_settable("CRM Lead", LEAD_FIELD, vertical=GRAIN["vertical"], group=GRAIN["group"])
		cls.partner = partner_fixture.make_partner("zz-lead-fields-partner@example.invalid", GRAIN, [f"lead:{LEAD_FIELD}"])

	@staticmethod
	def _task_type(type_name, read_only=0):
		return frappe.get_doc({
			"doctype": "CRM Task Type", "type_name": type_name,
			"vertical": GRAIN["vertical"], "group": GRAIN["group"], "program": GRAIN["program"],
			"schema": [
				{"label": "ZZ Lead Job Title", "fieldname": LEAD_FIELD, "fieldtype": "Data", "source": "Lead",
				 "read_only": read_only},
				{"label": "ZZ Activity Note", "fieldname": ACTIVITY_FIELD, "fieldtype": "Data"},
			],
		}).insert(ignore_permissions=True).name

	def setUp(self):
		frappe.set_user("Administrator")
		self.lead = frappe.get_doc({
			"doctype": "CRM Lead", "first_name": "ZZ Lead Fields Probe",
			"mobile_no": f"+91900001{int(frappe.generate_hash(length=8), 16) % 10000:04d}",
			"custom_vertical": GRAIN["vertical"], "custom_group": GRAIN["group"],
			"custom_current_program": GRAIN["program"], LEAD_FIELD: PREFILLED,
		}).insert(ignore_permissions=True)

	def _lead_value(self):
		return frappe.db.get_value("CRM Lead", self.lead.name, LEAD_FIELD)

	def _fields(self, task_type):
		return {f["fieldname"]: f for f in activity_api.type_config(task_type, lead=self.lead.name)["fields"]}

	def test_the_form_opens_with_the_lead_s_current_value(self):
		self.assertEqual(activity_api.type_config(self.task_type, lead=self.lead.name)["lead_values"].get(LEAD_FIELD),
						 PREFILLED)
		self.assertEqual(activity_api.type_config(self.task_type)["lead_values"], {}, "a lead-less form carried lead values")

	def test_a_lead_field_opens_editable(self):
		self.assertEqual(self._fields(self.task_type)[LEAD_FIELD]["read_only"], 0)

	def test_an_answer_the_rep_changes_goes_back_to_the_lead(self):
		activity_api.save_activity(self.lead.name, self.task_type, {LEAD_FIELD: CHANGED, ACTIVITY_FIELD: "note"})
		self.assertEqual(self._lead_value(), CHANGED)

	def test_a_field_the_rep_leaves_alone_keeps_its_value(self):
		activity_api.save_activity(self.lead.name, self.task_type, {ACTIVITY_FIELD: "note"})
		self.assertEqual(self._lead_value(), PREFILLED)

	def test_a_past_activity_keeps_its_answer_after_the_lead_changes(self):
		task = activity_api.save_activity(self.lead.name, self.task_type, {LEAD_FIELD: CHANGED, ACTIVITY_FIELD: "note"})
		lead = frappe.get_doc("CRM Lead", self.lead.name)
		lead.set(LEAD_FIELD, LATER)
		lead.save(ignore_permissions=True)
		self.assertEqual(activity_api.task_detail(task)["task"]["values"].get(LEAD_FIELD), CHANGED)

	def test_a_partner_api_activity_moves_the_lead_as_a_rep_s_does(self):
		partner_fixture.create_activity(self.partner, self.lead.name, "ZZ Lead Fields Probe",
										{LEAD_FIELD: CHANGED, ACTIVITY_FIELD: "note"})
		self.assertEqual(self._lead_value(), CHANGED)

	def test_a_field_declared_read_only_is_shown_but_never_written(self):
		task_type = self._task_type("ZZ Lead Fields Read Only Probe", read_only=1)
		self.assertEqual(self._fields(task_type)[LEAD_FIELD]["read_only"], 1)
		activity_api.save_activity(self.lead.name, task_type, {LEAD_FIELD: CHANGED, ACTIVITY_FIELD: "note"})
		self.assertEqual(self._lead_value(), PREFILLED)

	def test_a_lead_field_the_catalog_does_not_make_writable_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			lead_detail.write_lead_fields(self.lead.name, {"custom_patient_id": "ZZ Forged"})
