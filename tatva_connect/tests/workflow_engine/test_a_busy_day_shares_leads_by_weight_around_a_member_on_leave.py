# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A busy day: with the 75-weight member on leave, 300 leads split 180 / 120 between the 15 and the 10, and none is left unassigned."""

from collections import Counter

from frappe.tests import freeze_time

from tatva_connect.tests.workflow_engine import fixtures as fx


class TestABusyDaySharesLeadsByWeightAroundAMemberOnLeave(fx.PoolTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.fifteen, cls.ten, cls.away = fx.rep("busy-15"), fx.rep("busy-10"), fx.rep("busy-75")
		cls.rule = fx.pool([(cls.fifteen, 15, None), (cls.ten, 10, None), (cls.away, 75, None)])
		fx.leave(cls.away, fx.MONDAY)
		cls.leads = fx.leads(300)

	def test_three_hundred_leads_split_180_120_and_none_leaves_by_nobody(self):
		with freeze_time(fx.at(fx.MONDAY, 10)):
			drawn = [fx.distribute(self.rule, lead)[:2] for lead in self.leads]
		self.assertEqual(Counter(output for output, _user in drawn), {"assigned": 300})
		self.assertEqual(Counter(user for _output, user in drawn), {self.fifteen: 180, self.ten: 120})
