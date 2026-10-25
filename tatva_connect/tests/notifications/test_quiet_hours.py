# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A person's quiet hours: no push and no email inside them, in their own time zone, and no other email held; only the switch and FCM are stubbed."""
import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

import frappe
from frappe.desk.doctype.notification_settings.notification_settings import create_notification_settings
from frappe.tests.utils import FrappeTestCase

from tatva_connect.notifications import dispatch, push, quiet_hours, sender

REP = "zz-quiet-hours@example.com"
EVERY_DAY = list(quiet_hours.WEEK)


class TestQuietHours(FrappeTestCase):
	def setUp(self):
		self.addCleanup(frappe.db.rollback)
		self.enterContext(patch.object(dispatch, "armed", return_value=True))
		if not frappe.db.exists("User", REP):
			frappe.get_doc({"doctype": "User", "email": REP, "first_name": "Quiet Hours", "send_welcome_email": 0,
			                "time_zone": "Asia/Kolkata"}).insert(ignore_permissions=True)
		if not frappe.db.exists("Notification Settings", REP):
			create_notification_settings(REP)

	def _set(self, start, stop, days=EVERY_DAY):
		doc = frappe.get_doc("Notification Settings", REP)
		doc.set(quiet_hours.FROM, start)
		doc.set(quiet_hours.TO, stop)
		doc.set(quiet_hours.DAYS, [{"day": d} for d in days])
		doc.save(ignore_permissions=True)

	def test_a_window_past_midnight_belongs_to_the_day_it_starts_on(self):
		ist, start, stop = ZoneInfo("Asia/Kolkata"), datetime.time(21), datetime.time(7)
		fri_late = datetime.datetime(2026, 10, 23, 23, 0, tzinfo=ist)  # a Friday
		sat_early = datetime.datetime(2026, 10, 24, 6, 0, tzinfo=ist)
		sat_late = datetime.datetime(2026, 10, 24, 23, 0, tzinfo=ist)
		self.assertEqual(quiet_hours.window_end(fri_late, start, stop, {"Friday"}), datetime.datetime(2026, 10, 24, 7, 0, tzinfo=ist))
		self.assertEqual(quiet_hours.window_end(sat_early, start, stop, {"Friday"}), datetime.datetime(2026, 10, 24, 7, 0, tzinfo=ist))
		self.assertIsNone(quiet_hours.window_end(sat_late, start, stop, {"Friday"}), "Saturday night is not chosen")

	def test_the_persons_own_time_zone_decides(self):
		self._set("00:00", "23:59")
		self.assertIn(REP, quiet_hours.quiet_until([REP]))
		user = frappe.get_doc("User", REP)
		user.time_zone = "America/New_York"
		user.save(ignore_permissions=True)
		self._set("09:00", "10:00")
		at_nine_ny = datetime.datetime(2026, 10, 24, 9, 30, tzinfo=ZoneInfo("America/New_York"))
		site = at_nine_ny.astimezone(ZoneInfo(frappe.utils.get_system_timezone())).replace(tzinfo=None)
		with patch.object(quiet_hours, "now_datetime", return_value=site):
			self.assertIn(REP, quiet_hours.quiet_until([REP]), "09:30 in New York is inside 09:00-10:00 there")

	def test_blank_hours_or_the_switch_off_hold_nothing(self):
		self.assertNotIn(REP, quiet_hours.quiet_until([REP]))
		self._set("00:00", "23:59")
		with patch.object(dispatch, "armed", return_value=False):
			self.assertEqual(quiet_hours.quiet_until([REP]), {})

	def test_inside_the_window_push_is_skipped(self):
		self._set("00:00", "23:59")
		row = frappe.get_doc({"doctype": "Notification Log", "type": "Mention", "subject": "q", "for_user": REP}).insert(ignore_permissions=True)
		frappe.db.set_value("Notification Log", row.name, push.STATE, push.PENDING)
		with (
			patch.object(push, "_wanted", return_value={REP: {"Mention"}}),
			patch.object(sender, "devices", return_value={REP: ["t"]}),
			patch.object(sender, "configured", return_value=True),
			patch.object(sender, "send_to_tokens") as sent,
		):
			push.drain()
		self.assertEqual(frappe.db.get_value("Notification Log", row.name, push.STATE), push.QUIET)
		self.assertFalse([c for c in sent.call_args_list if c.args[0] == ["t"]])

	def test_a_notice_email_waits_for_the_window_and_other_email_does_not(self):
		self._set("00:00", "23:59")
		until = quiet_hours.quiet_until([REP])[REP]
		with patch("frappe.desk.doctype.notification_log.notification_log.is_email_notifications_enabled_for_type", return_value=True):
			frappe.get_doc({"doctype": "Notification Log", "type": "Mention", "subject": "held", "for_user": REP}).insert(ignore_permissions=True)
		frappe.sendmail(recipients=REP, subject="reset your password", message="x")
		held = frappe.db.get_value("Email Queue", {"message": ["like", "%held%"]}, "send_after", order_by="creation desc")
		free = frappe.db.get_value("Email Queue", {"message": ["like", "%reset your password%"]}, "send_after", order_by="creation desc")
		self.assertEqual(held, until)
		self.assertIsNone(free)
