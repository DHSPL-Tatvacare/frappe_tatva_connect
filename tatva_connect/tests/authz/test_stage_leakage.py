# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A lead never holds another programme's stage: the picker never offers one, and with the switch armed a save
carries it to the same-labelled own stage or refuses it. Own stages and lead, on Nivolumab and Tukavo."""

import frappe
from frappe.tests import IntegrationTestCase

from tatva_connect.lead.leads import lead_stages
from tatva_connect.tests.authz.grains import GRAINS, assert_masters_exist

SWITCH = "Lead::CRM Lead::stage"
_OWN = GRAINS[0]  # Goodflip-Care · Anaya · Nivolumab
_FOREIGN = GRAINS[1]  # Goodflip-Care · Anaya · Tukavo: same vertical and group, another programme


def _stage(program, label="ZZ Leak Probe"):
	return frappe.get_doc({"doctype": "CRM Lead Stage", "program": program, "stage": label, "selectable": 1}).insert(
		ignore_permissions=True).name


class TestAStageNeverCrossesItsProgramme(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		assert_masters_exist()
		cls.own_stage = _stage(_OWN["program"])
		cls.foreign_stage = _stage(_FOREIGN["program"])
		cls.foreign_only = _stage(_FOREIGN["program"], "ZZ Leak Tukavo Only")
		cls.lead = frappe.get_doc({
			"doctype": "CRM Lead", "first_name": "ZZ Stage Leak", "custom_vertical": _OWN["vertical"],
			"custom_group": _OWN["group"], "custom_current_program": _OWN["program"],
		}).insert(ignore_permissions=True).name
		cls._switch_was = frappe.db.get_value("CRM Tatva Automation", SWITCH, "enabled")

	@classmethod
	def tearDownClass(cls):
		cls._set_switch(cls._switch_was)
		super().tearDownClass()

	@staticmethod
	def _set_switch(enabled):
		row = frappe.get_doc("CRM Tatva Automation", SWITCH)
		row.enabled = enabled
		row.save(ignore_permissions=True)

	def test_the_picker_offers_only_the_leads_programme(self):
		names = {s["name"] for s in lead_stages(self.lead)}
		self.assertIn(self.own_stage, names)
		self.assertNotIn(self.foreign_stage, names, "another programme's stage leaked into the picker")

	def test_a_save_never_stores_another_programmes_stage(self):
		self._set_switch(1)
		lead = frappe.get_doc("CRM Lead", self.lead)
		lead.custom_substage = self.foreign_stage
		lead.save(ignore_permissions=True)
		self.assertEqual(frappe.db.get_value("CRM Lead", self.lead, "custom_substage"), self.own_stage,
		                 "a same-label foreign stage must be carried to this programme's own stage")
		lead.reload()
		lead.custom_substage = self.foreign_only
		with self.assertRaises(frappe.ValidationError):
			lead.save(ignore_permissions=True)
