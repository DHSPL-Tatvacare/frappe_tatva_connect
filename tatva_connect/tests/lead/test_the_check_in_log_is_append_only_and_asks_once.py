# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The check-in log: one prompt per login while on shift, a manager's change told once, rows never edited, a repeat never written, and no rep writes another's row."""

import frappe
from frappe.tests import freeze_time, set_user

from tatva_connect.api import checkin as checkin_api
from tatva_connect.tests.workflow_engine import fixtures as fx


def _rows(user):
	return frappe.db.count("Tatva User Checkin", {"user": user})


class TestTheCheckInLogIsAppendOnlyAndAsksOnce(fx.PoolTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		# New users each run: the once-per-login marks live in redis under the session id, which a rollback cannot clear.
		cls.prompted, cls.told, cls.meddler, cls.target, cls.repeat, cls.edited = (
			fx.rep(f"log-{i}") for i in ("prompted", "told", "meddler", "target", "repeat", "edited")
		)
		cls.manager = fx.rep("log-manager", "Sales Manager")
		# Check-in is a business-line switch, and a rep is entitled to the line through their pool.
		line = frappe.get_doc("CRM Vertical", fx.GRAIN["vertical"])
		line.checkin_enabled = 1
		line.save()
		fx.pool([(cls.prompted, 1, fx.shift("09:00:00", "17:00:00"))], require_checkin=1)
		fx.pool([(cls.told, 1, None)])
		with set_user(cls.edited):
			checkin_api.set_status("Active")

	def test_on_shift_and_not_checked_in_the_rep_is_prompted_once_per_login(self):
		with set_user(self.prompted):
			with freeze_time(fx.at(fx.MONDAY, 7)):
				self.assertFalse(checkin_api.get_status()["prompt"], "off shift, nothing to ask")
			with freeze_time(fx.at(fx.MONDAY, 10)):
				self.assertTrue(checkin_api.get_status()["prompt"])
				self.assertFalse(checkin_api.get_status()["prompt"], "asked twice in one login")

	def test_a_rep_cannot_write_another_reps_status(self):
		with set_user(self.meddler), self.assertRaises(frappe.PermissionError):
			frappe.get_doc({"doctype": "Tatva User Checkin", "user": self.target, "status": "Unavailable"}).insert()
		self.assertEqual(_rows(self.target), 0)

	def test_a_managers_change_is_marked_manager_and_told_to_the_rep_once(self):
		with set_user(self.manager):
			row = frappe.get_doc({"doctype": "Tatva User Checkin", "user": self.told, "status": "Active"}).insert()
		self.assertEqual(row.source, "Manager")
		with set_user(self.told):
			card = checkin_api.get_status()
			self.assertEqual((card["notice"], card["changed_by"], card["status"]), (True, self.manager, "Active"))
			self.assertFalse(checkin_api.get_status()["notice"], "told twice in one login")

	def test_a_check_in_row_is_never_edited(self):
		row = frappe.get_doc("Tatva User Checkin", {"user": self.edited})
		row.status = "Away"
		with self.assertRaises(frappe.ValidationError):
			row.save(ignore_permissions=True)

	def test_setting_the_same_status_twice_writes_one_row(self):
		with set_user(self.repeat):
			checkin_api.set_status("Away")
			checkin_api.set_status("Away")
		self.assertEqual(_rows(self.repeat), 1)
