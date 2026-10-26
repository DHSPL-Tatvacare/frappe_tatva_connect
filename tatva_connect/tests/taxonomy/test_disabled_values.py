# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A disabled stage, source or picklist value is retired, not deleted: no picker offers it, a lead already
holding it keeps it and saves, and a lead cannot be moved onto a disabled stage."""

import frappe
from frappe.desk.search import search_link
from frappe.tests import IntegrationTestCase

from tatva_connect.lead.leads import lead_stages
from tatva_connect.taxonomy import picklist
from tatva_connect.tests.authz.grains import GRAINS, assert_masters_exist

SWITCH = "Lead::CRM Lead::stage"
_G = GRAINS[0]
_GRAIN = (_G["vertical"], _G["group"], _G["program"])
_CATEGORY = "condition"


def _stage(label):
	return frappe.get_doc({"doctype": "CRM Lead Stage", "program": _G["program"], "stage": label}).insert(
		ignore_permissions=True).name


def _option(value):
	return frappe.get_doc({"doctype": "CRM Picklist Value", "category": _CATEGORY, "value": value,
	                       "vertical": _G["vertical"], "group": _G["group"], "program": _G["program"]}).insert(
		ignore_permissions=True).name


def _source(name):
	return frappe.get_doc({"doctype": "CRM Lead Source", "source_name": name}).insert(ignore_permissions=True).name


def _disable(doctype, name):
	doc = frappe.get_doc(doctype, name)
	doc.disabled = 1
	doc.save(ignore_permissions=True)


def _lead(stage, source, option):
	return frappe.get_doc({
		"doctype": "CRM Lead", "first_name": "ZZ Disabled Values", "custom_vertical": _G["vertical"],
		"custom_group": _G["group"], "custom_current_program": _G["program"],
		"custom_substage": stage, "source": source, "custom_condition": option,
	}).insert(ignore_permissions=True).name


class TestADisabledValueIsRetiredNotDeleted(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		assert_masters_exist()
		cls._switch_was = frappe.db.get_value("CRM Tatva Automation", SWITCH, "enabled")
		cls._set_switch(1)
		cls.live_stage, cls.gone_stage = _stage("ZZ Live Stage"), _stage("ZZ Gone Stage")
		cls.live_source, cls.gone_source = _source("ZZ Live Source"), _source("ZZ Gone Source")
		cls.live_option, cls.gone_option = _option("ZZ Live Option"), _option("ZZ Gone Option")
		# Set before the values are disabled, as a lead that stood there the day the business retired them.
		cls.holder = _lead(cls.gone_stage, cls.gone_source, cls.gone_option)
		cls.other = _lead(cls.live_stage, cls.live_source, cls.live_option)
		_disable("CRM Lead Stage", cls.gone_stage)
		_disable("CRM Lead Source", cls.gone_source)
		_disable("CRM Picklist Value", cls.gone_option)

	@classmethod
	def tearDownClass(cls):
		cls._set_switch(cls._switch_was)
		super().tearDownClass()

	@staticmethod
	def _set_switch(enabled):
		row = frappe.get_doc("CRM Tatva Automation", SWITCH)
		row.enabled = enabled
		row.save(ignore_permissions=True)

	def test_no_picker_offers_a_disabled_value(self):
		stages = {s["name"] for s in lead_stages(self.other)}
		self.assertIn(self.live_stage, stages)
		self.assertNotIn(self.gone_stage, stages)
		self.assertNotIn("ZZ Gone Stage", picklist._stage_values(_GRAIN, "custom_substage"))

		sources = {row["value"] for row in search_link("CRM Lead Source", "ZZ ", page_length=50)}
		self.assertIn(self.live_source, sources)
		self.assertNotIn(self.gone_source, sources)

		offered = {row[0] for row in picklist.picklist_query(
			"CRM Picklist Value", "", "name", 0, 50, {"category": _CATEGORY, "lead": self.other})}
		self.assertIn(self.live_option, offered)
		self.assertNotIn(self.gone_option, offered)
		self.assertNotIn("ZZ Gone Option", picklist.allowed_values(_CATEGORY, _GRAIN))
		self.assertNotIn(self.gone_option, {r.name for r in picklist.offered_rows(_CATEGORY, _GRAIN)})

	def test_a_lead_holding_one_keeps_it_and_saves(self):
		self.assertIn(self.gone_stage, {s["name"] for s in lead_stages(self.holder)}, "the pill must still name the lead's stage")
		asked = picklist.picklist_query("CRM Picklist Value", self.gone_option, "name", 0, 50,
		                                {"category": _CATEGORY, "lead": self.holder})
		self.assertIn(self.gone_option, {row[0] for row in asked}, "a control asking for its held key must still get the label")
		lead = frappe.get_doc("CRM Lead", self.holder)
		lead.first_name = "ZZ Disabled Values Edited"
		lead.save(ignore_permissions=True)
		held = frappe.db.get_value("CRM Lead", self.holder, ["custom_substage", "source", "custom_condition"])
		self.assertEqual(held, (self.gone_stage, self.gone_source, self.gone_option))

	def test_a_lead_cannot_be_moved_onto_a_disabled_stage(self):
		lead = frappe.get_doc("CRM Lead", self.other)
		lead.custom_substage = self.gone_stage
		with self.assertRaises(frappe.ValidationError):
			lead.save(ignore_permissions=True)
