# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Credit Weighted and the workflow pool flag on `TatvaAssignmentRule`, with core's own `AssignmentRule` as the oracle."""

from collections import Counter
from itertools import groupby
from unittest.mock import patch

import frappe
from frappe.automation.doctype.assignment_rule.assignment_rule import AssignmentRule
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, today

from tatva_connect.lead.assignment_rule import CREDIT_WEIGHTED
from tatva_connect.workflow_engine.tests import fixtures as fx


def _longest_run(picks):
	return max(len(list(run)) for _user, run in groupby(picks))


class TestCreditWeightedRule(FrappeTestCase):
	def setUp(self):
		fx.roll_back_pools(self)
		self.a, self.b, self.c, self.d = (fx.make_user(f"cw-probe-{i}@example.invalid") for i in "abcd")
		self.lead = fx.make_lead()

	def _picks(self, pool, count):
		return [pool.get_user(self.lead) for _ in range(count)]

	def _credits(self, pool):
		return frappe.parse_json(frappe.db.get_value("Assignment Rule", pool.name, "credits"))

	def test_picks_hold_the_exact_weight_ratio_and_credits_return_to_zero_each_cycle(self):
		pool = fx.make_pool([
			{"user": self.a, "weight": 8}, {"user": self.b, "weight": 7},
			{"user": self.c, "weight": 5}, {"user": self.d, "weight": 2},
		])
		for _cycle in range(2):
			self.assertEqual(Counter(self._picks(pool, 22)), {self.a: 8, self.b: 7, self.c: 5, self.d: 2})
			self.assertEqual(set(self._credits(pool).values()), {0})

	def test_picks_interleave_where_core_weighted_distribution_runs_in_blocks(self):
		members = [{"user": self.a, "weight": 5}, {"user": self.b, "weight": 3}, {"user": self.c, "weight": 2}]
		ours = self._picks(fx.make_pool(members), 10)
		core = self._picks(fx.make_pool(members, rule="Weighted Distribution"), 10)
		self.assertEqual(Counter(ours), Counter(core))
		self.assertEqual((_longest_run(ours), _longest_run(core)), (2, 5))

	def test_equal_weights_are_an_exact_round_robin(self):
		pool = fx.make_pool([{"user": u, "weight": 1} for u in (self.a, self.b, self.c)])
		self.assertEqual(self._picks(pool, 6), [self.a, self.b, self.c] * 2)

	def test_a_paused_member_is_skipped_and_rejoins_without_a_burst(self):
		pool = fx.make_pool([{"user": u, "weight": 1} for u in (self.a, self.b, self.c)])
		pool.weighted_users[2].paused = 1
		pool.save(ignore_permissions=True)
		self.assertNotIn(self.c, self._picks(pool, 4))
		self.assertEqual(self._credits(pool)[self.c], 0)

		pool.reload()
		pool.weighted_users[2].paused = 0
		pool.save(ignore_permissions=True)
		self.assertEqual(Counter(self._picks(pool, 3)), {self.a: 1, self.b: 1, self.c: 1})

	def test_a_disabled_user_is_never_picked(self):
		pool = fx.make_pool([{"user": u, "weight": 1} for u in (self.a, self.b)])
		user = frappe.get_doc("User", self.b)
		user.enabled = 0
		user.save(ignore_permissions=True)
		self.assertEqual(set(self._picks(pool, 4)), {self.a})

	def test_a_member_not_entitled_to_the_leads_grain_is_skipped(self):
		pool = fx.make_pool([{"user": u, "weight": 1} for u in (self.a, self.b)])
		with patch("tatva_connect.access.entitlement.grain_entitled", side_effect=lambda grain, user=None: user != self.b) as asked:
			self.assertEqual(set(self._picks(pool, 4)), {self.a})
		self.assertIn(fx.AXES, [call.args[0] for call in asked.call_args_list], "asked about some grain other than the lead's own")

	def test_the_daily_cap_counts_every_pick_today_including_cancelled_ones(self):
		pool = fx.make_pool([{"user": self.a, "weight": 1, "daily_cap": 2}, {"user": self.b, "weight": 1}])
		for _ in range(6):
			self.assertTrue(pool.do_assignment(self.lead.as_dict()))
		given = Counter(frappe.get_all("ToDo", filters={"assignment_rule": pool.name}, pluck="allocated_to"))
		self.assertEqual(given, {self.a: 2, self.b: 4})

		with patch("tatva_connect.lead.assignment_rule.today", return_value=add_days(today(), 1)):
			self.assertIn(self.a, self._picks(pool, 2))

	def test_nobody_who_can_take_a_lead_means_no_pick_and_no_assignment(self):
		pool = fx.make_pool([{"user": u, "weight": 1, "paused": 1} for u in (self.a, self.b)])
		self.assertIsNone(pool.get_user(self.lead))
		self.assertFalse(pool.do_assignment(self.lead.as_dict()))

	def test_the_pick_reads_credits_from_the_database_not_a_cached_doc(self):
		pool = fx.make_pool([{"user": u, "weight": 1} for u in (self.a, self.b, self.c)])
		stale = frappe.get_doc("Assignment Rule", pool.name)
		self.assertEqual([pool.get_user(self.lead), stale.get_user(self.lead), pool.get_user(self.lead)], [self.a, self.b, self.c])

	def test_a_workflow_pool_never_assigns_on_save_and_the_flag_is_the_only_switch(self):
		for flag, assigned in ((1, False), (0, True)):
			with self.subTest(assigned_by_workflow=flag):
				pool = fx.make_pool([{"user": self.a}], rule="Round Robin", priority=999, assigned_by_workflow=flag)
				fx.make_lead()
				self.assertEqual(bool(frappe.db.exists("ToDo", {"assignment_rule": pool.name})), assigned)

	def test_a_core_strategy_is_exactly_core(self):
		for strategy in ("Round Robin", "Load Balancing"):
			with self.subTest(rule=strategy):
				pool = fx.make_pool([{"user": u} for u in (self.a, self.b)], rule=strategy)
				self.assertEqual(pool.get_user(self.lead), AssignmentRule.get_user(pool, self.lead))

	def test_the_strategy_is_merged_into_cores_options_not_replacing_them(self):
		options = frappe.get_meta("Assignment Rule").get_field("rule").options.split("\n")
		for option in ("Round Robin", "Load Balancing", "Based on Field", "Weighted Distribution", CREDIT_WEIGHTED):
			self.assertIn(option, options)

	def test_the_daily_cap_count_is_indexed(self):
		self.assertTrue(frappe.db.has_index("tabToDo", "ix_todo_allocated_rule_creation"))
