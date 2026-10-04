# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A rep holding the pool's limit of untouched tasks gets no new lead until they close one; a pool with no limit set hands out as before (DA57)."""

import frappe

from tatva_connect.tests.workflow_engine import fixtures as fx


class TestARepAtTheUntouchedLimitGetsNoNewLead(fx.PoolTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.rep = fx.rep("limit-rep")
		cls.pool = fx.pool([(cls.rep, 1, None)], max_untouched=2)
		cls.first, cls.second, cls.third, cls.fourth = fx.leads(4)
		cls.free_rep = fx.rep("no-limit-rep")
		cls.free_pool = fx.pool([(cls.free_rep, 1, None)])

	def test_the_rep_waits_at_the_limit_and_gets_the_next_lead_after_closing_a_task(self):
		tasks = []
		for lead in (self.first, self.second):
			self.assertEqual(fx.distribute(self.pool, lead)[1], self.rep)
			tasks.append(fx.task(lead.name, self.rep))
		self.assertEqual(fx.distribute(self.pool, self.third)[:2], ("nobody", None))
		tasks[0].status = "Done"
		tasks[0].save(ignore_permissions=True)  # authz-ok: tier-c — test fixture, the rep closing their task
		self.assertEqual(fx.distribute(self.pool, self.fourth)[1], self.rep)

	def test_a_pool_with_no_limit_set_keeps_handing_out(self):
		for lead in fx.leads(4):
			self.assertEqual(fx.distribute(self.free_pool, lead)[1], self.free_rep)
			fx.task(lead.name, self.free_rep)
