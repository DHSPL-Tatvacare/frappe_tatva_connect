# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Credit Weighted hands out turns in the exact weight ratio, interleaved, skipping whoever cannot take a lead now."""

import datetime
from collections import Counter
from itertools import groupby

import frappe
from frappe.tests import freeze_time

from tatva_connect.lead.assignment_rule import TatvaAssignmentRule
from tatva_connect.tests.workflow_engine import fixtures as fx
from tatva_connect.workflow_engine.tests.fixtures import make_lead, make_pool


def _longest_run(picks):
	return max(len(list(run)) for _user, run in groupby(picks))


class TestCreditWeightedTurns(fx.PoolTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.a, cls.b, cls.c, cls.d = (fx.rep(f"cw-{i}") for i in "abcd")
		cls.lead = make_lead()
		cls.ratio = fx.pool([(cls.a, 8, None), (cls.b, 7, None), (cls.c, 5, None), (cls.d, 2, None)])
		three = [(cls.a, 5, None), (cls.b, 3, None), (cls.c, 2, None)]
		cls.smooth, cls.core_weighted = fx.pool(three), fx.pool(three, rule="Weighted Distribution")
		cls.paused = fx.pool([(u, 1, None) for u in (cls.a, cls.b, cls.c)])
		cls.stale = fx.pool([(u, 1, None) for u in (cls.a, cls.b, cls.c)])
		cls.capped = fx.pool([(cls.a, 1, None, {"daily_cap": 2}), (cls.b, 1, None)])
		gone = fx.rep("cw-disabled")
		cls.with_disabled = fx.pool([(cls.a, 1, None), (gone, 1, None)])
		user = frappe.get_doc("User", gone)
		user.enabled = 0
		user.save(ignore_permissions=True)

	def _picks(self, rule, count):
		return [rule.get_user(self.lead) for _ in range(count)]

	def test_picks_hold_the_exact_weight_ratio_and_credits_return_to_zero_each_cycle(self):
		for _cycle in range(2):
			self.assertEqual(Counter(self._picks(self.ratio, 22)), {self.a: 8, self.b: 7, self.c: 5, self.d: 2})
			self.assertEqual(set(fx.credits(self.ratio).values()), {0})

	def test_picks_interleave_where_core_weighted_distribution_runs_in_blocks(self):
		ours, core = self._picks(self.smooth, 10), self._picks(self.core_weighted, 10)
		self.assertEqual(Counter(ours), Counter(core))
		self.assertEqual((_longest_run(ours), _longest_run(core)), (2, 5))

	def test_a_paused_member_is_skipped_and_rejoins_without_a_burst(self):
		self.paused.weighted_users[2].paused = 1
		self.paused.save(ignore_permissions=True)
		self.assertNotIn(self.c, self._picks(self.paused, 4))
		self.assertEqual(fx.credits(self.paused)[self.c], 0)

		self.paused.reload()
		self.paused.weighted_users[2].paused = 0
		self.paused.save(ignore_permissions=True)
		self.assertEqual(Counter(self._picks(self.paused, 3)), {self.a: 1, self.b: 1, self.c: 1})

	def test_a_disabled_user_is_never_picked(self):
		self.assertEqual(set(self._picks(self.with_disabled, 4)), {self.a})

	def test_the_daily_cap_stops_a_member_for_the_day_and_frees_them_the_next(self):
		with freeze_time(fx.at(fx.MONDAY, 10)):
			# Core's `do_assignment` cancels the lead's previous ToDo each time; cancelled ones still count toward the cap.
			for _ in range(6):
				self.assertTrue(self.capped.do_assignment(self.lead.as_dict()))
		given = Counter(frappe.get_all("ToDo", filters={"assignment_rule": self.capped.name}, pluck="allocated_to"))
		self.assertEqual(given, {self.a: 2, self.b: 4})
		with freeze_time(fx.at(fx.MONDAY + datetime.timedelta(days=1), 10)):
			self.assertIn(self.a, self._picks(self.capped, 2))

	def test_the_pick_reads_credits_from_the_database_not_a_cached_doc(self):
		stale = frappe.get_doc("Assignment Rule", self.stale.name)
		self.assertEqual(
			[self.stale.get_user(self.lead), stale.get_user(self.lead), self.stale.get_user(self.lead)], [self.a, self.b, self.c]
		)


class TestPoolsOnASave(fx.PoolTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.a, cls.b = fx.rep("save-a"), fx.rep("save-b")
		cls.lead = make_lead()
		cls.core_pools = {s: make_pool([{"user": u} for u in (cls.a, cls.b)], rule=s, assigned_by_workflow=1) for s in ("Round Robin", "Load Balancing")}

	def test_a_workflow_pool_never_assigns_on_save_and_the_flag_is_the_only_switch(self):
		for flag, assigned in ((1, False), (0, True)):
			with self.subTest(assigned_by_workflow=flag):
				rule = make_pool([{"user": self.a}], rule="Round Robin", priority=999, assigned_by_workflow=flag)
				make_lead()
				self.assertEqual(bool(frappe.db.exists("ToDo", {"assignment_rule": rule.name})), assigned)

	def test_a_core_strategy_picks_exactly_as_the_rule_it_extends(self):
		for strategy, rule in self.core_pools.items():
			with self.subTest(rule=strategy):
				self.assertEqual(rule.get_user(self.lead), super(TatvaAssignmentRule, rule).get_user(self.lead))
