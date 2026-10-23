# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Every notice the CRM grid offers can reach email: a rep who ticks it is mailed, asked of frappe's own email gate.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \\
        --module tatva_connect.tests.notifications.test_email_channel
"""
import frappe
from frappe.desk.doctype.notification_settings.notification_settings import (
	create_notification_settings,
	is_email_notifications_enabled_for_type,
)
from frappe.tests.utils import FrappeTestCase

from tatva_connect.notifications import api

_REP = "email-channel-probe@example.invalid"


class TestEmailChannel(FrappeTestCase):
	def setUp(self):
		if not frappe.db.exists("User", _REP):
			frappe.get_doc({"doctype": "User", "email": _REP, "first_name": "Email Probe", "send_welcome_email": 0}).insert(ignore_permissions=True)
		if not frappe.db.exists(api.USER_SETTINGS, _REP):
			create_notification_settings(_REP)

	def tearDown(self):
		frappe.db.rollback()

	def test_every_grid_type_ticked_for_email_is_mailed(self):
		types = [row["type"] for row in api._grid(frappe.get_doc(api.USER_SETTINGS, _REP)) if row["push"] is not None]
		doc = frappe.get_doc(api.USER_SETTINGS, _REP)
		doc.set(api._EMAIL_MASTER, 1)
		doc.set(api.EMAIL_TYPES_FIELD, [{"notification_type": t} for t in types])
		doc.save(ignore_permissions=True)
		refused = [t for t in types if not is_email_notifications_enabled_for_type(_REP, t)]
		self.assertEqual(refused, [], "a notice the grid offers for email must reach email when ticked")
