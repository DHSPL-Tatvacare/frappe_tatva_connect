# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The tray's only delete.

Two rules it exists to hold: the switch ships OFF and a dormant sweep touches nothing, and an UNREAD row
is never purged however old — it is someone's outstanding work item, and age is not consent.
"""
import unittest.mock

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.notifications import retention

USER = "retention-reader@example.com"


def _row(read, days_old):
	name = frappe.get_doc(
		{"doctype": "CRM Notification", "to_user": USER, "type": "Assignment", "read": read}
	).insert(ignore_permissions=True).name
	stamp = frappe.utils.add_to_date(frappe.utils.now_datetime(), days=-days_old)
	frappe.db.set_value("CRM Notification", name, "creation", stamp, update_modified=False)
	return name


class TestTrayRetention(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		if not frappe.db.exists("User", USER):
			frappe.get_doc(
				{"doctype": "User", "email": USER, "first_name": "Retention", "send_welcome_email": 0}
			).insert(ignore_permissions=True)

	def setUp(self):
		frappe.db.delete("CRM Notification", {"to_user": USER})
		self._armed(0)

	def tearDown(self):
		frappe.db.delete("CRM Notification", {"to_user": USER})
		self._armed(0)

	def _armed(self, enabled):
		frappe.db.set_value(
			"CRM Tatva Automation", retention.SWITCH_RETENTION, "enabled", enabled, update_modified=False
		)
		frappe.clear_cache()

	def test_a_dormant_sweep_deletes_nothing(self):
		old_read = _row(read=1, days_old=200)
		self.assertFalse(retention.purge_read_notifications()["ok"])
		self.assertTrue(frappe.db.exists("CRM Notification", old_read))

	def test_an_armed_sweep_takes_read_rows_past_the_floor(self):
		old_read = _row(read=1, days_old=200)
		self._armed(1)
		retention.purge_read_notifications(days=90)
		self.assertFalse(frappe.db.exists("CRM Notification", old_read))

	def test_it_keeps_a_read_row_inside_the_floor(self):
		recent_read = _row(read=1, days_old=10)
		self._armed(1)
		retention.purge_read_notifications(days=90)
		self.assertTrue(frappe.db.exists("CRM Notification", recent_read))

	def test_it_never_takes_an_unread_row_however_old(self):
		old_unread = _row(read=0, days_old=500)
		self._armed(1)
		retention.purge_read_notifications(days=90)
		self.assertTrue(frappe.db.exists("CRM Notification", old_unread))

	def test_it_refuses_a_retention_that_would_empty_the_tray(self):
		# days=0 puts the floor at NOW, which matches every read row on the site — the one input that turns housekeeping into a wipe.
		old_read = _row(read=1, days_old=200)
		self._armed(1)
		result = retention.purge_read_notifications(days=0)
		self.assertFalse(result["ok"])
		self.assertTrue(frappe.db.exists("CRM Notification", old_read))

	def test_it_deletes_across_batches(self):
		names = [_row(read=1, days_old=200) for _ in range(5)]
		self._armed(1)
		with unittest.mock.patch.object(retention, "BATCH", 2):
			result = retention.purge_read_notifications(days=90)
		self.assertEqual(result["deleted"], 5)
		self.assertFalse(any(frappe.db.exists("CRM Notification", n) for n in names))

	def test_the_switch_ships_dormant(self):
		self.assertFalse(
			frappe.db.get_value("CRM Tatva Automation", retention.SWITCH_RETENTION, "enabled"),
			"the retention row must ship OFF — an operator arms it",
		)
