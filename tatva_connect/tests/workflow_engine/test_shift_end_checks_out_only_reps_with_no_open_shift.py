# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The shift-end job checks out an Active rep none of whose check-in pools has a shift open, and no one else."""

from frappe.tests import freeze_time, set_user

from tatva_connect.api import checkin as checkin_api
from tatva_connect.lead import checkin
from tatva_connect.tests.workflow_engine import fixtures as fx


class TestShiftEndChecksOutOnlyRepsWithNoOpenShift(fx.PoolTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		day, evening = fx.shift("09:00:00", "17:00:00"), fx.shift("17:00:00", "23:00:00")
		cls.ended, cls.still_on, cls.no_pool, cls.on_break = (fx.rep(f"shift-end-{i}") for i in ("ended", "still-on", "no-pool", "break"))
		for user, status in ((cls.ended, "Active"), (cls.still_on, "Active"), (cls.no_pool, "Active"), (cls.on_break, "Away")):
			with set_user(user):
				checkin_api.set_status(status)
		fx.pool([(cls.ended, 1, day), (cls.still_on, 1, day), (cls.on_break, 1, day)], require_checkin=1)
		fx.pool([(cls.still_on, 1, evening)], require_checkin=1)
		# A pool that keeps shifts but does not require check-in never checks anyone out.
		fx.pool([(cls.no_pool, 1, day)])

	def test_only_the_active_rep_with_no_open_shift_is_checked_out(self):
		with freeze_time(fx.at(fx.MONDAY, 18)):
			checkin.close_ended_shifts()
		statuses = {user: (fx.latest_checkin(user).status, fx.latest_checkin(user).source)
		            for user in (self.ended, self.still_on, self.no_pool, self.on_break)}
		self.assertEqual(statuses, {
			self.ended: ("Unavailable", "Shift End"),
			self.still_on: ("Active", "Self"),
			self.no_pool: ("Active", "Self"),
			self.on_break: ("Away", "Self"),
		})
