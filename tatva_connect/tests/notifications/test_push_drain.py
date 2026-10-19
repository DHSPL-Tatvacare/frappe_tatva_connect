# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Push reads the Notification Log row: the drain sends a row to a rep who picked its type, once, and nothing while off.

Only the outside world is stubbed: whether CRM Push Settings is filled, and the FCM transport. The rows, the rep's
own push list and their device are real, written through the document API.
"""
from unittest import mock

import frappe
from frappe.desk.doctype.notification_log.notification_log import enqueue_create_notification
from frappe.desk.doctype.notification_settings.notification_settings import create_notification_settings
from frappe.tests.utils import FrappeTestCase

from tatva_connect.notifications import api, dispatch, push, sender

_REP = "push-drain-probe@example.invalid"
_TOKEN = "fcm-token-push-drain-probe"


class TestPushDrain(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		if not frappe.db.exists("User", _REP):
			frappe.get_doc({"doctype": "User", "email": _REP, "first_name": "Push Probe", "send_welcome_email": 0}).insert(ignore_permissions=True)

	def setUp(self):
		frappe.db.delete("Notification Log", {"for_user": _REP})
		frappe.db.delete(sender.SUBSCRIPTION, {"fcm_token": _TOKEN})
		frappe.get_doc({"doctype": sender.SUBSCRIPTION, "user": _REP, "fcm_token": _TOKEN}).insert(ignore_permissions=True)
		self._pick(dispatch.MISSED_CALL)
		self._armed = dispatch.armed
		dispatch.armed = lambda: True

	def tearDown(self):
		dispatch.armed = self._armed

	def _pick(self, *types):
		"""The rep's own push master and push list, through the document API."""
		if not frappe.db.exists(api.USER_SETTINGS, _REP):
			create_notification_settings(_REP)
		doc = frappe.get_doc(api.USER_SETTINGS, _REP)
		doc.set(api.PUSH_MASTER, 1)
		doc.set(api.PUSH_TYPES_FIELD, [{"notification_type": t} for t in types])
		doc.save(ignore_permissions=True)

	def _row(self, bell_type):
		enqueue_create_notification(_REP, {"type": bell_type, "subject": f"a {bell_type}", "from_user": "Administrator"})
		return frappe.get_all("Notification Log", filters={"for_user": _REP, "type": bell_type}, pluck="name")[0]

	def _drain(self):
		with mock.patch.object(sender, "configured", return_value=True), mock.patch.object(sender, "send_to_tokens") as sent:
			push.drain()
		return sent

	def test_a_picked_type_is_pushed_once(self):
		row = self._row(dispatch.MISSED_CALL)
		first, second = self._drain(), self._drain()
		self.assertEqual(first.call_count, 1)
		self.assertEqual(first.call_args.args[0], [_TOKEN])
		self.assertEqual(second.call_count, 0, "a row is pushed once, however many passes see it")
		self.assertEqual(frappe.db.get_value("Notification Log", row, push.STATE), push.SENT)

	def test_a_type_the_rep_did_not_pick_is_skipped(self):
		row = self._row(dispatch.TASK_OVERDUE)
		self.assertEqual(self._drain().call_count, 0)
		self.assertEqual(frappe.db.get_value("Notification Log", row, push.STATE), push.SKIPPED)

	def test_switch_off_reads_no_row(self):
		dispatch.armed = lambda: False
		row = self._row(dispatch.MISSED_CALL)
		with mock.patch.object(frappe, "get_all") as read:
			self.assertEqual(self._drain().call_count, 0)
		read.assert_not_called()
		self.assertFalse(frappe.db.get_value("Notification Log", row, push.STATE), "a row the drain never read carries no stamp")
