# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""At the 17:00 handover the morning members' credits freeze and the evening members share; in every window no member runs ahead of its weight share by more than one."""

import datetime
from collections import Counter

from frappe.tests import freeze_time

from tatva_connect.tests.workflow_engine import fixtures as fx

TUESDAY = fx.MONDAY + datetime.timedelta(days=1)


class TestThe1700HandoverFreezesTheMorningAndSharesTheEvening(fx.PoolTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		morning, evening = fx.shift("09:00:00", "17:00:00"), fx.shift("17:00:00", "01:00:00")
		cls.morning = {fx.rep("handover-m5"): 5, fx.rep("handover-m3"): 3}
		cls.evening = {fx.rep("handover-e4"): 4, fx.rep("handover-e1"): 1}
		members = [(u, w, morning) for u, w in cls.morning.items()] + [(u, w, evening) for u, w in cls.evening.items()]
		# One pool and one set of leads per test, so neither test's draws move the other's credits.
		cls.frozen_rule, cls.smooth_rule = fx.pool(members), fx.pool(members)
		# Seven morning draws leave the morning credits uneven, so a freeze is visible.
		cls.frozen_windows = ((fx.at(fx.MONDAY, 16), fx.leads(7)), (fx.at(fx.MONDAY, 17), fx.leads(10)))
		cls.smooth_windows = ((fx.at(fx.MONDAY, 16), cls.morning, fx.leads(7)), (fx.at(fx.MONDAY, 17), cls.evening, fx.leads(10)),
		                      (fx.at(TUESDAY, 9), cls.morning, fx.leads(8)))

	@staticmethod
	def _window(rule, when, leads):
		with freeze_time(when):
			return [fx.distribute(rule, lead)[1] for lead in leads]

	def _assert_smooth(self, picks, weights):
		total = sum(weights.values())
		for drawn in range(1, len(picks) + 1):
			counts = Counter(picks[:drawn])
			for user, weight in weights.items():
				self.assertLessEqual(counts[user], drawn * weight / total + 1, f"{user} ran ahead after {drawn} draws: {picks}")

	def test_the_evening_shares_while_the_morning_credits_stay_frozen(self):
		(first, early), (handover, late) = self.frozen_windows
		self._window(self.frozen_rule, first, early)
		frozen = {user: fx.credits(self.frozen_rule)[user] for user in self.morning}
		self.assertNotEqual(set(frozen.values()), {0}, "seven draws should leave the morning credits uneven")

		picks = self._window(self.frozen_rule, handover, late)
		self.assertEqual(set(picks), set(self.evening))
		self.assertEqual({user: fx.credits(self.frozen_rule)[user] for user in self.morning}, frozen)

	def test_no_member_runs_ahead_of_its_weight_share_by_more_than_one_in_any_window(self):
		for when, weights, leads in self.smooth_windows:
			with self.subTest(window=when):
				picks = self._window(self.smooth_rule, when, leads)
				self.assertTrue(set(picks) <= set(weights), f"{picks} drew outside the shift open at {when}")
				self._assert_smooth(picks, weights)
