# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A saved view is scoped to one grain: a one-grain user has it filled in, a two-grain user must name it, and a
System Manager may leave it open. Real users entitled through real Assignment Rules; nothing patched."""

import frappe
from frappe.tests import IntegrationTestCase

from tatva_connect.smartview import api as smartview

VERTICAL = "ZZ One Grain Vertical"
GROUP_ONE = "ZZ One Grain Group One"
GROUP_TWO = "ZZ One Grain Group Two"
ONE_GRAIN = "onegrain.single@example.test"
TWO_GRAINS = "onegrain.double@example.test"
FLAG = "Access::Grain::registry"


def _rule(user, group):
	frappe.get_doc({"doctype": "Assignment Rule", "name": f"zz-one-grain-{user}-{group}", "document_type": "CRM Lead",
	                "assign_condition": "1", "rule": "Round Robin", "priority": 0, "disabled": 0,
	                "grain_vertical": VERTICAL, "grain_group": group, "users": [{"user": user}],
	                "assignment_days": [{"day": "Monday"}]}).insert(ignore_permissions=True)


class TestAViewIsScopedToOneGrain(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.get_doc({"doctype": "CRM Vertical", "vertical_name": VERTICAL}).insert(ignore_permissions=True)
		for group in (GROUP_ONE, GROUP_TWO):
			frappe.get_doc({"doctype": "CRM Group", "group_name": group}).insert(ignore_permissions=True)
		for user in (ONE_GRAIN, TWO_GRAINS):
			frappe.get_doc({"doctype": "User", "email": user, "first_name": "One Grain", "send_welcome_email": 0,
			                "roles": [{"role": "Sales User"}]}).insert(ignore_permissions=True)
		_rule(ONE_GRAIN, GROUP_ONE)
		_rule(TWO_GRAINS, GROUP_ONE)
		_rule(TWO_GRAINS, GROUP_TWO)
		cls._flag_was = frappe.db.get_value("CRM Tatva Automation", FLAG, "enabled")
		cls._set_flag(0)  # entitlement comes from the Assignment Rules above

	@classmethod
	def tearDownClass(cls):
		cls._set_flag(cls._flag_was)
		super().tearDownClass()

	@staticmethod
	def _set_flag(enabled):
		row = frappe.get_doc("CRM Tatva Automation", FLAG)
		row.enabled = enabled
		row.save(ignore_permissions=True)

	def setUp(self):
		if hasattr(frappe.local, "tatva_connect:entitled_grains"):
			delattr(frappe.local, "tatva_connect:entitled_grains")

	def _save(self, user, label, axes=None):
		with self.set_user(user):
			return frappe.get_doc("CRM Smart View", smartview.upsert_view({"label": label, "base_object": "Lead", **(axes or {})})["name"])

	def test_a_two_grain_user_must_name_the_grain(self):
		with self.assertRaises(frappe.ValidationError):
			self._save(TWO_GRAINS, "ZZ One Grain Unscoped")

	def test_a_two_grain_user_gets_the_grain_they_name(self):
		doc = self._save(TWO_GRAINS, "ZZ One Grain Named", {"vertical": VERTICAL, "group": GROUP_TWO})
		self.assertEqual((doc.vertical, doc.group), (VERTICAL, GROUP_TWO))

	def test_a_one_grain_user_has_it_filled_in(self):
		doc = self._save(ONE_GRAIN, "ZZ One Grain Sole")
		self.assertEqual((doc.vertical, doc.group), (VERTICAL, GROUP_ONE))

	def test_a_system_manager_may_leave_it_open(self):
		doc = frappe.get_doc("CRM Smart View", smartview.upsert_view(
			{"label": "ZZ One Grain Admin", "base_object": "Lead", "is_standard": 1})["name"])
		self.assertFalse(doc.vertical or doc.group or doc.program)
