# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Push reads the Notification Log row: the drain sends a row to a rep who picked its type, once, and nothing while off.

Only the outside world is stubbed: whether CRM Push Settings is filled, and the FCM transport. The rows, the rep's
own push list and their device are real, written through the document API.
"""
import time
from unittest import mock

import frappe
from frappe.desk.doctype.notification_log.notification_log import enqueue_create_notification
from frappe.desk.doctype.notification_settings.notification_settings import create_notification_settings
from frappe.tests.utils import FrappeTestCase

from tatva_connect import presence
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
		"""One pass; returns only the sends to the probe's device, so other bench reps' rows never move the count."""
		with mock.patch.object(sender, "configured", return_value=True), mock.patch.object(sender, "credentials", return_value=("p", "t")), mock.patch.object(sender, "send_to_tokens") as sent:
			push.drain()
		return [c for c in sent.call_args_list if c.args[0] == [_TOKEN]]

	def test_a_picked_type_is_pushed_once(self):
		row = self._row(dispatch.MISSED_CALL)
		first, second = self._drain(), self._drain()
		self.assertEqual(len(first), 1)
		self.assertEqual(len(second), 0, "a row is pushed once, however many passes see it")
		self.assertEqual(frappe.db.get_value("Notification Log", row, push.STATE), push.SENT)

	def test_a_type_the_rep_did_not_pick_is_skipped(self):
		row = self._row(dispatch.TASK_OVERDUE)
		self.assertEqual(len(self._drain()), 0)
		self.assertEqual(frappe.db.get_value("Notification Log", row, push.STATE), push.SKIPPED)

	def test_switch_off_reads_no_row(self):
		dispatch.armed = lambda: False
		row = self._row(dispatch.MISSED_CALL)
		with mock.patch.object(frappe, "get_all") as read:
			self.assertEqual(len(self._drain()), 0)
		read.assert_not_called()
		self.assertFalse(frappe.db.get_value("Notification Log", row, push.STATE), "a row the drain never read carries no stamp")

	def test_a_pass_reads_the_key_once(self):
		self._pick(dispatch.MISSED_CALL, dispatch.TASK_OVERDUE)
		self._row(dispatch.MISSED_CALL), self._row(dispatch.TASK_OVERDUE)
		ok = mock.Mock(status_code=200)
		with (
			mock.patch.object(sender, "configured", return_value=True),
			mock.patch.object(sender, "_service_account_info", return_value={"project_id": "p"}) as key,
			mock.patch.object(sender, "_access_token", return_value="t"),
			mock.patch.object(sender, "_post_one", return_value=ok) as posted,
		):
			push.drain()
		self.assertEqual(len([c for c in posted.call_args_list if c.args[2] == _TOKEN]), 2)
		self.assertEqual(key.call_count, 1, "the Firebase key is decrypted once per pass, not once per row")

	def test_a_push_expires_with_its_notice(self):
		self._row(dispatch.MISSED_CALL)
		with (
			mock.patch.object(sender, "configured", return_value=True),
			mock.patch.object(sender, "credentials", return_value=("p", "t")),
			mock.patch.object(sender.requests, "post", return_value=mock.Mock(status_code=200)) as post,
		):
			push.drain()
		message = next(c.kwargs["json"]["message"] for c in post.call_args_list if c.kwargs["json"]["message"]["token"] == _TOKEN)
		self.assertTrue(0 < int(message["webpush"]["headers"]["TTL"]) <= sender.STALE_SECONDS, "FCM holds a push no longer than the notice stays news")
		self.assertEqual(message["webpush"]["headers"]["Urgency"], "high")
		self.assertEqual(message["data"]["tag"], dispatch.MISSED_CALL, "one banner per notice type on the device")

	def test_a_rep_in_the_crm_is_not_pushed(self):
		row = self._row(dispatch.MISSED_CALL)
		self._tab(presence.ACTIVE)
		try:
			self.assertEqual(len(self._drain()), 0, "the bell rings in front of them; no device buzzes too")
		finally:
			frappe.cache.delete_value(presence._key(_REP))
		self.assertEqual(frappe.db.get_value("Notification Log", row, push.STATE), push.SKIPPED)

	def test_a_rep_idle_in_the_crm_is_pushed(self):
		self._row(dispatch.MISSED_CALL)
		self._tab(presence.IDLE)
		try:
			self.assertEqual(len(self._drain()), 1, "an idle tab is not someone watching the bell")
		finally:
			frappe.cache.delete_value(presence._key(_REP))

	def test_a_rep_whose_tab_went_quiet_is_pushed(self):
		self._row(dispatch.MISSED_CALL)
		frappe.cache.hset(presence._key(_REP), "probe-device|probe-tab", {"state": presence.ACTIVE, "at": time.time() - presence.PRESENCE_TTL_SECONDS - 1})
		try:
			self.assertEqual(len(self._drain()), 1, "a tab not heard from inside the TTL is not someone here")
		finally:
			frappe.cache.delete_value(presence._key(_REP))

	def _tab(self, state):
		"""One open CRM tab of the rep's, written through the endpoint a browser calls."""
		frappe.set_user(_REP)
		try:
			presence.mark(state, device_id="probe-device", tab_id="probe-tab")
		finally:
			frappe.set_user("Administrator")
