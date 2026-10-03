# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A rep holding the pool's limit of untouched tasks gets no new lead until they close one; the limit is never below one (DA55)."""

import frappe

from tatva_connect.tests.workflow_engine import fixtures as fx


class TestARepAtTheUntouchedLimitGetsNoNewLead(fx.PoolTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.rep = fx.rep("limit-rep")
		cls.pool = fx.pool([(cls.rep, 1, None)], max_untouched=2)
		cls.first, cls.second, cls.third, cls.fourth = fx.leads(4)

	def test_the_rep_waits_at_the_limit_and_gets_the_next_lead_after_closing_a_task(self):
		tasks = []
		for lead in (self.first, self.second):
			self.assertEqual(fx.distribute(self.pool, lead)[1], self.rep)
			tasks.append(fx.task(lead.name, self.rep))
		self.assertEqual(fx.distribute(self.pool, self.third)[:2], ("nobody", None))
		tasks[0].status = "Done"
		tasks[0].save(ignore_permissions=True)  # authz-ok: tier-c — test fixture, the rep closing their task
		self.assertEqual(fx.distribute(self.pool, self.fourth)[1], self.rep)

	def test_a_limit_below_one_is_refused(self):
		pool = frappe.get_doc("Assignment Rule", self.pool.name)
		pool.max_untouched = 0
		with self.assertRaises(frappe.ValidationError):
			pool.save(ignore_permissions=True)  # authz-ok: tier-c — test fixture, a manager's bad edit
