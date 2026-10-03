# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A pool never hands a lead to a member whose User Permissions do not reach the lead's grain."""

import frappe

from tatva_connect.tests.workflow_engine import fixtures as fx
from tatva_connect.workflow_engine.tests.fixtures import GRAIN, make_lead


class TestAPoolSkipsAMemberWhoMayNotSeeTheLead(fx.PoolTestCase):
	registry = 1

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		grain = {"vertical": GRAIN["vertical"], "group": GRAIN["group"], "program": GRAIN["program"]}
		if not frappe.db.exists("CRM Grain", grain):
			frappe.get_doc({"doctype": "CRM Grain", **grain}).insert(ignore_permissions=True)
		cls.seen, cls.blind = fx.rep("entitled"), fx.rep("not-entitled")
		# Granted the way production grants it with the registry on: a User Permission per axis, for CRM Lead.
		for allow, value in (("CRM Vertical", GRAIN["vertical"]), ("CRM Group", GRAIN["group"])):
			frappe.get_doc({"doctype": "User Permission", "user": cls.seen, "allow": allow, "for_value": value,
			                "applicable_for": "CRM Lead", "apply_to_all_doctypes": 0}).insert(ignore_permissions=True)
		cls.rule = fx.pool([(cls.blind, 1, None), (cls.seen, 1, None)])
		cls.leads = fx.leads(3)

	def test_only_the_member_who_may_see_the_lead_receives_it(self):
		self.assertEqual({fx.distribute(self.rule, lead)[:2] for lead in self.leads}, {("assigned", self.seen)})
