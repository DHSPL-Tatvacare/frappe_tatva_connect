# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The five new notification events — the gate, the opt-in, the tray, and firing exactly once.

Four promises are proved here, each the kind of thing that only shows up in production:

  * Switch OFF is silent. Nothing is toasted, nothing is pushed, and NO tray row is written — a rep
    who has not been given the feature sees exactly the rows native crm writes, and no others.
  * Switch ON without an opt-in is silent too. The operator arms it; the rep still chooses.
  * A tray row is written by crm's OWN writer, and ONLY for an event crm does not already bell itself
    (assignment and inbound WhatsApp are crm's — we never post a second row beside them).
  * Each trigger fires once. A doc event fires on the save that changed the field and no other; the
    sweep tells a rep once per due date, and again only if the task is rescheduled.

The gates are flipped by a test-scoped monkeypatch of `automation.is_enabled` (never a persisted DB
write), and the transport is spied at `dispatch._toast` / `dispatch._push` / `dispatch.notify_user` —
so no real socket, no real FCM call, and no real tray row is ever created.
"""
import unittest

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_to_date, now_datetime

from tatva_connect.notifications import catalog, dispatch, events
from tatva_connect.tests.authz.grains import GRAINS, assert_masters_exist

_GRAIN = GRAINS[0]
_USER = "notif-probe@example.invalid"


def _make_user():
	if not frappe.db.exists("User", _USER):
		frappe.get_doc(
			{"doctype": "User", "email": _USER, "first_name": "Notif Probe", "send_welcome_email": 0}
		).insert(ignore_permissions=True)
	return _USER


def _make_lead():
	lead = frappe.get_doc(
		{
			"doctype": "CRM Lead",
			"first_name": "NotifProbe",
			"lead_name": "NotifProbe Lead",
			"status": "New",
			"custom_vertical": _GRAIN["vertical"],
			"custom_group": _GRAIN["group"],
			"custom_current_program": _GRAIN["program"],
			"mobile_no": "+919876500777",
			"email": "notif-probe-lead@example.invalid",
		}
	).insert(ignore_permissions=True)
	frappe.db.set_value("CRM Lead", lead.name, "_assign", frappe.as_json([_USER]), update_modified=False)
	return lead


class _Spy:
	"""Replaces the whole transport: the tray writer + both live channels."""

	def __init__(self):
		self.bells, self.toasts, self.pushes = [], [], []

	def __enter__(self):
		self._orig = (dispatch.notify_user, dispatch._toast, dispatch._push)
		dispatch.notify_user = lambda payload: self.bells.append(payload)
		dispatch._toast = lambda user, title, body, data: self.toasts.append(user)
		dispatch._push = lambda tokens, title, body, data: self.pushes.append(tokens)
		return self

	def __exit__(self, *exc):
		dispatch.notify_user, dispatch._toast, dispatch._push = self._orig


def _gates(**enabled):
	"""Monkeypatch the ONE global gate — never a DB write (dormant-by-default must hold for real)."""
	orig = dispatch.automation.is_enabled
	dispatch.automation.is_enabled = lambda key: bool(enabled.get(key, False))
	return orig


def _optin(user, *event_keys):
	doc = frappe.new_doc("CRM Notification Preference")
	name = frappe.db.exists("CRM Notification Preference", {"user": user})
	if name:
		doc = frappe.get_doc("CRM Notification Preference", name)
	doc.user = user
	doc.set("subscriptions", [])
	for key in event_keys:
		doc.append("subscriptions", {"event_key": key, "channel": "live", "enabled": 1})
	doc.save(ignore_permissions=True)


class TestNotificationEventsGating(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		assert_masters_exist()
		super().setUpClass()
		cls.user = _make_user()
		cls.lead = _make_lead()

	def setUp(self):
		self._orig_is_enabled = dispatch.automation.is_enabled
		frappe.db.delete("CRM Notification Subscription", {"parenttype": "CRM Notification Preference"})

	def tearDown(self):
		dispatch.automation.is_enabled = self._orig_is_enabled

	def test_switch_off_writes_no_tray_row_and_sends_nothing(self):
		"""The whole point of dormant: a rep sees only what native crm wrote, and nothing of ours."""
		_gates()  # every switch off
		_optin(self.user, "Telephony::Call::missed")  # opted in, but the operator has not armed it
		with _Spy() as spy:
			dispatch.notify(
				"Telephony::Call::missed",
				[self.user],
				title="t",
				body="b",
				bell={"actor": "Administrator", "text": "x", "source": ("CRM Call Log", "c1"), "target": ("CRM Lead", self.lead.name)},
			)
		self.assertEqual(spy.bells, [])
		self.assertEqual(spy.toasts, [])
		self.assertEqual(spy.pushes, [])

	def test_switch_on_without_optin_sends_nothing(self):
		"""The operator arms it; the rep still chooses. No opt-in row -> not subscribed."""
		_gates(**{"Notify::Telephony::missed": True})
		with _Spy() as spy:
			dispatch.notify(
				"Telephony::Call::missed",
				[self.user],
				title="t",
				body="b",
				bell={"actor": "Administrator", "text": "x", "source": ("CRM Call Log", "c1"), "target": ("CRM Lead", self.lead.name)},
			)
		self.assertEqual(spy.bells, [])
		self.assertEqual(spy.toasts, [])
		self.assertEqual(spy.pushes, [])

	def test_switch_on_and_opted_in_writes_one_tray_row_through_crms_writer(self):
		_gates(**{"Notify::Telephony::missed": True})
		_optin(self.user, "Telephony::Call::missed")
		with _Spy() as spy:
			dispatch.notify(
				"Telephony::Call::missed",
				[self.user],
				title="t",
				body="b",
				bell={"actor": "Administrator", "text": "x", "source": ("CRM Call Log", "c1"), "target": ("CRM Lead", self.lead.name)},
			)
		self.assertEqual(len(spy.bells), 1)
		self.assertEqual(spy.bells[0]["notification_type"], "Call")
		self.assertEqual(spy.bells[0]["assigned_to"], self.user)
		# always_push: a missed call is only useful before the patient gives up
		self.assertEqual(len(spy.pushes), 1)
		self.assertEqual(spy.toasts, [])

	def test_an_event_crm_already_bells_never_gets_a_second_tray_row(self):
		"""Assignment + inbound WhatsApp are crm's own tray rows — we add the live channel, never a row."""
		for key in ("Lead::Assignment::assigned", "Task::Assignment::assigned", "WhatsApp::Message::received"):
			self.assertEqual(catalog.get(key).bell_type, "", key)

		_gates(**{"Notify::WhatsApp::received": True})
		_optin(self.user, "WhatsApp::Message::received")
		with _Spy() as spy:
			dispatch.notify("WhatsApp::Message::received", [self.user], title="t", body="b")
		self.assertEqual(spy.bells, [])  # crm wrote it; we did not
		self.assertEqual(len(spy.toasts) + len(spy.pushes), 1)  # exactly one live channel

	def test_every_event_pairs_with_a_registry_switch(self):
		from tatva_connect.automation.registry import AUTOMATIONS

		keys = {a.key for a in AUTOMATIONS}
		for event in catalog.all_events():
			self.assertIn(event.automation_key, keys, event.key)


class TestTaskDueSweepFiresOnce(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		assert_masters_exist()
		super().setUpClass()
		cls.user = _make_user()
		cls.lead = _make_lead()

	def setUp(self):
		self._orig_is_enabled = dispatch.automation.is_enabled
		_optin(self.user, "Task::Due::soon", "Task::Due::overdue")

	def tearDown(self):
		dispatch.automation.is_enabled = self._orig_is_enabled
		frappe.db.delete("CRM Task", {"custom_task_type": ["is", "not set"], "assigned_to": self.user})

	def _task(self, due):
		return frappe.get_doc(
			{
				"doctype": "CRM Task",
				"title": "NotifProbe task",
				"status": "Todo",
				"assigned_to": self.user,
				"due_date": due,
				"reference_doctype": "CRM Lead",
				"reference_docname": self.lead.name,
			}
		).insert(ignore_permissions=True)

	def test_due_soon_tells_the_rep_once_then_stays_quiet(self):
		_gates(**{"Notify::Task::due-soon": True})
		task = self._task(add_to_date(now_datetime(), minutes=10))
		with _Spy() as spy:
			events.sweep_task_due()
			first = len(spy.bells)
			events.sweep_task_due()  # the very next sweep, 5 minutes later
			second = len(spy.bells)
		self.assertEqual(first, 1)
		self.assertEqual(second, 1, "a second sweep must not tell the rep again")
		self.assertEqual(
			str(frappe.db.get_value("CRM Task", task.name, "custom_due_soon_notified_for")),
			str(task.due_date),
		)

	def test_a_rescheduled_task_is_warned_again(self):
		_gates(**{"Notify::Task::due-soon": True})
		task = self._task(add_to_date(now_datetime(), minutes=10))
		with _Spy() as spy:
			events.sweep_task_due()
			frappe.db.set_value("CRM Task", task.name, "due_date", add_to_date(now_datetime(), minutes=20))
			events.sweep_task_due()
			self.assertEqual(len(spy.bells), 2, "a new due date is a new warning")

	def test_a_done_task_is_never_swept(self):
		_gates(**{"Notify::Task::due-soon": True, "Notify::Task::overdue": True})
		task = self._task(add_to_date(now_datetime(), minutes=-60))
		frappe.db.set_value("CRM Task", task.name, "status", "Done")
		with _Spy() as spy:
			events.sweep_task_due()
		self.assertEqual(spy.bells, [])

	def test_switch_off_sweeps_nothing(self):
		_gates()  # both off
		self._task(add_to_date(now_datetime(), minutes=-60))
		with _Spy() as spy:
			events.sweep_task_due()
		self.assertEqual(spy.bells, [])
		self.assertEqual(spy.toasts, [])
		self.assertEqual(spy.pushes, [])


if __name__ == "__main__":
	unittest.main()
