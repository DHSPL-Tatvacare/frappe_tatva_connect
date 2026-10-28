# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A rep holding the pool's limit of untouched tasks gets no new lead until they close one; a pool with no limit set hands out as before (DA57)."""

import frappe

from tatva_connect.lead import routing
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

	def test_an_outbound_call_frees_the_slot_its_task_still_holds(self):
		rep = fx.rep("call-limit-rep")
		pool = fx.pool([(rep, 1, None)], max_untouched=1)
		first, second = fx.leads(2)
		self.assertEqual(fx.distribute(pool, first)[1], rep)
		fx.task(first.name, rep)
		self.assertEqual(fx.distribute(pool, second)[:2], ("nobody", None))
		frappe.get_doc({"doctype": "CRM Call Log", "id": f"probe-{frappe.generate_hash(length=10)}", "type": "Outgoing", "status": "Completed",
			"from": "+910000000000", "to": "+910000000001", "caller": rep, "reference_doctype": "CRM Lead",
			"reference_docname": first.name}).insert(ignore_permissions=True)  # authz-ok: tier-c — test fixture, the rep's call
		self.assertEqual(fx.distribute(pool, fx.leads(1)[0])[1], rep, "the call is lead work, so the task left open no longer holds the slot")

	def test_every_kind_of_lead_work_is_hooked(self):
		"""A kind of work without its doc_events lines would count but never wake a pool: the declaration is locked to the hooks."""
		events = frappe.get_hooks("doc_events")
		for doctype in routing.lead_work():
			self.assertIn("tatva_connect.lead.routing.on_lead_work", events.get(doctype, {}).get("on_update", []), doctype)

	def test_a_pool_with_no_limit_set_keeps_handing_out(self):
		for lead in fx.leads(4):
			self.assertEqual(fx.distribute(self.free_pool, lead)[1], self.free_rep)
			fx.task(lead.name, self.free_rep)
