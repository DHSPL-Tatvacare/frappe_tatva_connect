# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""On a holiday its grain covers, a pool leaves by `closed` with the next shift start, and assigns when that start comes."""

import datetime

from frappe.tests import freeze_time

from tatva_connect.tests.workflow_engine import fixtures as fx
from tatva_connect.workflow_engine.tests.fixtures import GRAIN

TUESDAY, WEDNESDAY = (fx.MONDAY + datetime.timedelta(days=n) for n in (1, 2))


class TestAHolidayClosesThePoolUntilTheNextOpening(fx.PoolTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.rule = fx.pool([(fx.rep(f"holiday-{i}"), 1, fx.shift("09:00:00", "17:00:00")) for i in "ab"])
		fx.holiday_list(GRAIN["vertical"], fx.MONDAY)
		fx.holiday_list(fx.OTHER_GRAIN["vertical"], WEDNESDAY)
		cls.parked, cls.elsewhere = fx.leads(2)

	def test_a_holiday_leaves_by_closed_with_the_next_opening_and_assigns_at_that_opening(self):
		with freeze_time(fx.at(fx.MONDAY, 10)):
			self.assertEqual(fx.distribute(self.rule, self.parked), ("closed", None, fx.at(TUESDAY, 9)))
		with freeze_time(fx.at(TUESDAY, 9)):
			self.assertEqual(fx.distribute(self.rule, self.parked)[0], "assigned")

	def test_another_grains_holiday_leaves_the_pool_open(self):
		with freeze_time(fx.at(WEDNESDAY, 10)):
			self.assertEqual(fx.distribute(self.rule, self.elsewhere)[0], "assigned")
