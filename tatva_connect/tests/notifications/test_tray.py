# Copyright (c) 2026, TatvaCare and Contributors. See license.txt
"""The CRM bell reads frappe's Notification Log: each notice once, frappe's gates, and only the CRM's own rows."""
import frappe
from crm.fcrm.doctype.crm_notification.crm_notification import notify_user
from frappe.desk.doctype.notification_log.notification_log import enqueue_create_notification
from frappe.desk.doctype.notification_settings.notification_settings import toggle_notifications
from frappe.tests.utils import FrappeTestCase

from tatva_connect.notifications import tray

DT = "CRM Smart View"
LABEL = "ZZ Tray View"
OWNER = "zz-tray-owner@example.com"
FRIEND = "zz-tray-friend@example.com"


class TestTheBellReadsOneStore(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		for email in (OWNER, FRIEND):
			if not frappe.db.exists("User", email):
				frappe.get_doc({"doctype": "User", "email": email, "first_name": email.split("@")[0],
				                "send_welcome_email": 0, "roles": [{"role": "Sales User"}]}).insert(ignore_permissions=True)

	def setUp(self):
		frappe.set_user("Administrator")
		self._purge()
		self.view = frappe.get_doc({"doctype": DT, "label": LABEL, "base_object": "Lead", "owner_user": OWNER}).insert(ignore_permissions=True).name

	def tearDown(self):
		frappe.set_user("Administrator")
		toggle_notifications(FRIEND, enable=True, ignore_permissions=True)
		self._purge()

	def _purge(self):
		frappe.db.delete("Notification Log", {"for_user": FRIEND})
		for name in frappe.get_all(DT, filters={"label": LABEL}, pluck="name"):
			frappe.db.delete("DocShare", {"share_doctype": DT, "share_name": name})
			frappe.delete_doc(DT, name, force=True, ignore_permissions=True)

	def _tray(self):
		frappe.set_user(FRIEND)
		try:
			return tray.get_notifications()
		finally:
			frappe.set_user("Administrator")

	def _share(self):
		frappe.set_user(OWNER)
		try:
			frappe.share.add(DT, self.view, FRIEND, read=1, notify=1)
		finally:
			frappe.set_user("Administrator")

	def test_a_share_appears_once_and_opens_the_view(self):
		self._share()
		items = self._tray()["items"]
		self.assertEqual(len(items), 1)
		self.assertEqual((items[0]["route_name"], items[0]["reference_name"]), ("SmartViews", self.view))

	def test_an_assignment_is_written_once(self):
		"""crm's writer leaves Assignment to frappe, so frappe's own notice is the only row."""
		notify_user({"owner": OWNER, "assigned_to": FRIEND, "notification_type": "Assignment", "notification_text": "x",
		             "redirect_to_doctype": DT, "redirect_to_docname": self.view})
		enqueue_create_notification(FRIEND, {"type": "Assignment", "subject": "x", "from_user": OWNER,
		                                     "document_type": DT, "document_name": self.view})
		self.assertEqual(len(self._tray()["items"]), 1)

	def test_nothing_when_system_notifications_are_off(self):
		toggle_notifications(FRIEND, enable=False, ignore_permissions=True)
		self._share()
		self.assertEqual(self._tray()["items"], [])

	def test_mark_all_read_touches_only_the_crms_rows(self):
		self._share()
		enqueue_create_notification(FRIEND, {"type": "Alert", "subject": "a course", "from_user": OWNER, "app": "lms"})
		frappe.set_user(FRIEND)
		try:
			tray.mark_as_read()
		finally:
			frappe.set_user("Administrator")
		self.assertEqual(self._tray()["unread"], 0)
		self.assertEqual(frappe.db.get_value("Notification Log", {"for_user": FRIEND, "app": "lms"}, "read"), 0)


class TestNotifyUserWritesNotificationLog(FrappeTestCase):
	"""crm's notify_user, with tatva_connect installed: frappe's Notification Log, and Assignment and Mention left to frappe."""

	def setUp(self):
		frappe.set_user("Administrator")
		for email in (OWNER, FRIEND):
			if not frappe.db.exists("User", email):
				frappe.get_doc({"doctype": "User", "email": email, "first_name": email.split("@")[0],
				                "send_welcome_email": 0}).insert(ignore_permissions=True)
		toggle_notifications(FRIEND, enable=True, ignore_permissions=True)
		frappe.db.delete("Notification Log", {"for_user": FRIEND})

	def tearDown(self):
		frappe.db.delete("Notification Log", {"for_user": FRIEND})

	def _payload(self, notification_type):
		return {"owner": OWNER, "assigned_to": FRIEND, "notification_type": notification_type,
		        "notification_text": "<span>an event</span>", "reference_doctype": "User", "reference_docname": FRIEND,
		        "redirect_to_doctype": "User", "redirect_to_docname": FRIEND}

	def test_a_notice_is_one_notification_log_row_and_no_crm_notification(self):
		notify_user(self._payload("WhatsApp"))
		self.assertEqual(frappe.db.count("Notification Log", {"for_user": FRIEND, "type": "WhatsApp"}), 1)
		self.assertEqual(frappe.db.count("CRM Notification", {"to_user": FRIEND}), 0)

	def test_assignment_and_mention_are_left_to_frappe(self):
		for kind in ("Assignment", "Mention"):
			notify_user(self._payload(kind))
		self.assertEqual(frappe.db.count("Notification Log", {"for_user": FRIEND}), 0)
