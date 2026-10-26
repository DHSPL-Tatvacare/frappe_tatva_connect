# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Assign To offers a lead only to the people the pool would hand it to: a Sales role, and an entitlement covering its grain."""

import frappe

from tatva_connect.lead.leads import assignable_users
from tatva_connect.tests.workflow_engine import fixtures as fx
from tatva_connect.workflow_engine.tests.fixtures import GRAIN, make_lead


class TestAssignToOffersOnlyWhoMayOwnTheLead(fx.PoolTestCase):
	registry = 1

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		grain = {"vertical": GRAIN["vertical"], "group": GRAIN["group"], "program": GRAIN["program"]}
		if not frappe.db.exists("CRM Grain", grain):
			frappe.get_doc({"doctype": "CRM Grain", **grain}).insert(ignore_permissions=True)  # authz-ok: tier-c — test fixture
		cls.seen, cls.blind = fx.rep("assignable"), fx.rep("not-entitled")
		# Every grain, but no Sales role: an administrator is not someone a lead is worked by.
		cls.admin = fx.rep("admin-only", "System Manager")
		# Granted the way production grants it with the registry on: a User Permission per axis, for CRM Lead.
		for allow, value in (("CRM Vertical", GRAIN["vertical"]), ("CRM Group", GRAIN["group"])):
			frappe.get_doc({"doctype": "User Permission", "user": cls.seen, "allow": allow, "for_value": value,
			                "applicable_for": "CRM Lead", "apply_to_all_doctypes": 0}).insert(ignore_permissions=True)  # authz-ok: tier-c — test fixture
		cls.lead = make_lead()

	def test_only_a_sales_user_covering_the_grain_is_offered(self):
		offered = {row.name for row in assignable_users(self.lead.name)}
		self.assertIn(self.seen, offered)
		self.assertNotIn(self.blind, offered)
		self.assertNotIn(self.admin, offered)
