# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Off shift, Distribute leaves by `closed` with the next opening; once the shift is open it assigns."""

from frappe.tests import freeze_time

from tatva_connect.tests.workflow_engine import fixtures as fx


class TestDistributeLeavesByClosedUntilTheShiftOpens(fx.PoolTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		a, b = fx.rep("shift-a"), fx.rep("shift-b")
		cls.day = fx.pool([(u, 1, fx.shift("08:00:00", "20:00:00")) for u in (a, b)])
		cls.night = fx.pool([(u, 1, fx.shift("22:00:00", "06:00:00")) for u in (a, b)])
		cls.day_lead, cls.after_midnight, cls.after_night = fx.leads(3)

	def test_off_shift_leaves_by_closed_until_the_shift_opens_then_assigns(self):
		with freeze_time(fx.at(fx.MONDAY, 2)):
			self.assertEqual(fx.distribute(self.day, self.day_lead), ("closed", None, fx.at(fx.MONDAY, 8)))
		with freeze_time(fx.at(fx.MONDAY, 8)):
			self.assertEqual(fx.distribute(self.day, self.day_lead)[0], "assigned")

	def test_a_shift_crossing_midnight_is_open_after_midnight_and_closed_after_it_ends(self):
		with freeze_time(fx.at(fx.MONDAY, 2)):
			self.assertEqual(fx.distribute(self.night, self.after_midnight)[0], "assigned")
		with freeze_time(fx.at(fx.MONDAY, 7)):
			self.assertEqual(fx.distribute(self.night, self.after_night), ("closed", None, fx.at(fx.MONDAY, 22)))
