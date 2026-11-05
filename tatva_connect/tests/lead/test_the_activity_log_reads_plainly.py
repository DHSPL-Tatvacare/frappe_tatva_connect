# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Each activity row says what happened in one line: a status change names who made it, an idle gap names its span and minutes."""

import datetime

import frappe
from frappe.tests import set_user
from frappe.utils import get_datetime

from tatva_connect.lead import checkin
from tatva_connect.tests.workflow_engine import fixtures as fx


class TestTheActivityLogReadsPlainly(fx.PoolTestCase):
	def test_a_status_change_and_an_idle_gap_read_plainly(self):
		user = fx.rep("log-headline")
		with set_user(user):
			active = checkin.record(user, checkin.ACTIVE)
		self.assertEqual(active.headline, f"{get_datetime(active.creation):%H:%M} · Set Active · by self")
		self.assertEqual(active.idle_minutes, 0)

		back = frappe.utils.now_datetime()
		idle = checkin.record_away(user, back - datetime.timedelta(minutes=54))
		self.assertEqual(idle.idle_minutes, 54)
		self.assertTrue(idle.headline.endswith(" · Idle 54 min"), idle.headline)

		out = checkin.record(user, checkin.UNAVAILABLE, checkin.SHIFT_END)
		self.assertEqual(out.headline, f"{get_datetime(out.creation):%H:%M} · Set Unavailable · by shift end")
