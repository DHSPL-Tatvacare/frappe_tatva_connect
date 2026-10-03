# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A lead a manager assigns by hand while its journey waits for the shift leaves Distribute by `assigned` with that person, costing no turn and no cap."""

import frappe
from frappe.desk.form import assign_to
from frappe.tests import freeze_time

from tatva_connect.tests.workflow_engine import fixtures as fx


class TestAManualReassignWhileParkedIsKept(fx.PoolTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		day = fx.shift("09:00:00", "17:00:00")
		cls.holder, cls.other = fx.rep("parked-holder"), fx.rep("parked-other")
		cls.rule = fx.pool([(cls.holder, 3, day, {"daily_cap": 5}), (cls.other, 1, day)])
		cls.lead = fx.leads(1)[0]

	def _cap_count(self):
		return frappe.db.count("ToDo", {"allocated_to": self.holder, "assignment_rule": self.rule.name})

	def test_the_managers_pick_is_kept_and_the_pool_turns_and_cap_are_untouched(self):
		with freeze_time(fx.at(fx.MONDAY, 3)):
			self.assertEqual(fx.distribute(self.rule, self.lead)[0], "closed")
		assign_to.add({"doctype": "CRM Lead", "name": self.lead.name, "assign_to": [self.holder]})
		turns, cap = fx.credits(self.rule), self._cap_count()

		with freeze_time(fx.at(fx.MONDAY, 9)):
			self.assertEqual(fx.distribute(self.rule, self.lead)[:2], ("assigned", self.holder))
		self.assertEqual(fx.credits(self.rule), turns)
		self.assertEqual(self._cap_count(), cap)
