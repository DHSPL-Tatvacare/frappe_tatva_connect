# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The notices we write — missed call, task due soon, task overdue — as real Notification Log rows.

Each promise is read off the row frappe stores, never off a spy on our own code:

  * Switch OFF writes nothing, and the sweep never even reads a task.
  * Switch ON writes ONE row per rep, of the notice's own Notification Type, through crm's `notify_user`.
  * Due soon reaches each rep by their own lead time, from their own Notification Settings row.
  * Each trigger fires once: a call that ARRIVES as No Answer never moved there; the sweep tells a rep once per
    due date, again only after a reschedule, and many tasks are one notice.

The gate is flipped by a test-scoped monkeypatch of `dispatch.armed` (never a persisted DB write). In tests frappe
writes the row inline (`enqueue_create_notification`, `now=frappe.in_test`), so the row is there on return.
"""
import unittest
from unittest import mock

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_to_date, now_datetime

from tatva_connect.notifications import api, dispatch, events
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
	existing = frappe.db.get_value("CRM Lead", {"mobile_no": "+919876500777"}, "name")  # an earlier run's probe lead; the dedup index refuses a second
	lead = frappe.get_doc("CRM Lead", existing) if existing else frappe.get_doc(
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
			"lead_owner": _USER,
		}
	).insert(ignore_permissions=True)
	if lead.lead_owner != _USER:  # an earlier run's probe lead; crm tells the owner of a lead nobody holds
		lead.lead_owner = _USER
		lead.save(ignore_permissions=True)
	return lead


def _gates(on):
	"""Flip the ONE switch for this test — never a DB write."""
	dispatch.armed = lambda: on


def _rows(bell_type):
	return frappe.get_all(
		"Notification Log", filters={"for_user": _USER, "type": bell_type}, fields=["subject", "document_type", "document_name", "link"]
	)


def _clear_rows():
	# The sweep commits per rep, so an earlier test's rows outlive its rollback.
	frappe.db.delete("Notification Log", {"for_user": _USER})


class _Probe(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		assert_masters_exist()
		super().setUpClass()
		cls.user = _make_user()
		cls.lead = _make_lead()

	def setUp(self):
		self._armed = dispatch.armed
		_clear_rows()

	def tearDown(self):
		dispatch.armed = self._armed
		_clear_rows()


class TestTheGate(_Probe):
	def test_switch_off_writes_no_row(self):
		_gates(False)
		dispatch.notify(dispatch.MISSED_CALL, [self.user], "Administrator", "x", ("CRM Lead", self.lead.name), ("CRM Lead", self.lead.name))
		self.assertEqual(_rows("Missed Call"), [])

	def test_a_type_we_do_not_write_is_refused(self):
		_gates(True)
		self.assertEqual(dispatch.notify("Assignment", [self.user], "Administrator", "x", ("CRM Lead", self.lead.name), ("CRM Lead", self.lead.name)), [])

	def test_switch_on_writes_one_row_of_the_notices_own_type(self):
		_gates(True)
		told = dispatch.notify(dispatch.MISSED_CALL, [self.user, self.user], "Administrator", "Missed call from +91", ("CRM Lead", self.lead.name), ("CRM Lead", self.lead.name))
		self.assertEqual(told, [self.user])
		rows = _rows("Missed Call")
		self.assertEqual(len(rows), 1, "one row per rep, however often they are named")
		self.assertEqual((rows[0].document_type, rows[0].document_name), ("CRM Lead", self.lead.name))

	def test_every_notice_writes_a_registered_notification_type(self):
		from tatva_connect.notifications import drift

		drift.assert_registered()  # the migrate gate itself: a missing switch, type or field throws


class TestArrivingIsNotMoving(_Probe):
	"""A call CREATED as No Answer has not MOVED to it: a reconcile backfill must not announce its batch."""

	def _call(self, status):
		return frappe.get_doc({
			"doctype": "CRM Call Log", "id": f"notif-{frappe.generate_hash(length=8)}",
			"type": "Incoming", "status": status, "telephony_medium": "Manual",
			"from": "+919876500777", "to": "+918035303509", "duration": 0,
			"reference_doctype": "CRM Lead", "reference_docname": self.lead.name,
		}).insert(ignore_permissions=True)  # authz-ok: tier-a — test fixture, runs as Administrator

	def test_a_call_backfilled_as_no_answer_tells_nobody(self):
		_gates(True)
		self._call("No Answer")
		self.assertEqual(_rows("Missed Call"), [], "a backfilled call was announced")

	def test_a_call_that_really_goes_unanswered_tells_its_rep_once(self):
		_gates(True)
		call = self._call("Ringing")
		call.status = "No Answer"
		call.save(ignore_permissions=True)  # authz-ok: tier-a — test drives the real status move
		call.save(ignore_permissions=True)  # authz-ok: tier-a — a later save of the same call
		self.assertEqual(len(_rows("Missed Call")), 1)


class TestTaskDueSweep(_Probe):
	def setUp(self):
		super().setUp()
		frappe.db.delete("CRM Task", {"custom_task_type": ["is", "not set"], "assigned_to": self.user})

	def tearDown(self):
		super().tearDown()
		frappe.db.delete("CRM Task", {"custom_task_type": ["is", "not set"], "assigned_to": self.user})

	def _lead(self, option):
		"""The rep's own lead time, through the document API."""
		from frappe.desk.doctype.notification_settings.notification_settings import (
			create_notification_settings,
		)

		if not frappe.db.exists(api.USER_SETTINGS, self.user):
			create_notification_settings(self.user)
		doc = frappe.get_doc(api.USER_SETTINGS, self.user)
		doc.set(api.LEAD_FIELD, option)
		doc.save(ignore_permissions=True)
		self.addCleanup(frappe.db.set_value, api.USER_SETTINGS, self.user, api.LEAD_FIELD, None)

	def _task(self, due, lead=True):
		return frappe.get_doc(
			{
				"doctype": "CRM Task",
				"title": "NotifProbe task",
				"status": "Todo",
				"assigned_to": self.user,
				"due_date": due,
				**({"reference_doctype": "CRM Lead", "reference_docname": self.lead.name} if lead else {}),
			}
		).insert(ignore_permissions=True)

	def test_switch_off_reads_no_task(self):
		_gates(False)
		with mock.patch.object(events, "_pending") as pending:
			events.sweep_reminders()
		pending.assert_not_called()

	def test_due_soon_tells_the_rep_once_then_stays_quiet(self):
		_gates(True)
		task = self._task(add_to_date(now_datetime(), minutes=10))
		events.sweep_reminders()
		events.sweep_reminders()  # the very next sweep, 5 minutes later
		rows = _rows("Task Due Soon")
		self.assertEqual(len(rows), 1, "a second sweep must not tell the rep again")
		self.assertEqual(rows[0].subject, "Due soon: NotifProbe task", "one plain sentence, no markup")
		self.assertEqual(str(frappe.db.get_value("CRM Task", task.name, "custom_due_soon_notified_for")), str(task.due_date))

	def test_each_rep_is_warned_by_their_own_lead_time(self):
		_gates(True)
		self._task(add_to_date(now_datetime(), hours=2))
		events.sweep_reminders()
		self.assertEqual(_rows("Task Due Soon"), [], "two hours out is outside the default hour")
		self._lead("3 hours")
		events.sweep_reminders()
		self.assertEqual(len(_rows("Task Due Soon")), 1, "the rep asked to hear three hours ahead")

	def test_a_rescheduled_task_is_warned_again(self):
		_gates(True)
		task = self._task(add_to_date(now_datetime(), minutes=10))
		events.sweep_reminders()
		frappe.db.set_value("CRM Task", task.name, "due_date", add_to_date(now_datetime(), minutes=20))
		events.sweep_reminders()
		self.assertEqual(len(_rows("Task Due Soon")), 2, "a new due date is a new warning")

	def test_several_overdue_tasks_are_one_notice_opening_the_drilled_list(self):
		_gates(True)
		tasks = [self._task(add_to_date(now_datetime(), minutes=-m)) for m in (10, 20, 30)]
		events.sweep_reminders()
		rows = _rows("Task Overdue")
		self.assertEqual(len(rows), 1, "three overdue tasks are one notice, not three")
		self.assertIn("/crm/tasks/view/list?filters=", rows[0].link)
		for task in tasks:
			self.assertEqual(str(frappe.db.get_value("CRM Task", task.name, "custom_overdue_notified_for")), str(task.due_date))

	def test_a_lone_task_without_a_lead_still_tells_its_rep(self):
		_gates(True)
		self._task(add_to_date(now_datetime(), minutes=-60), lead=False)
		events.sweep_reminders()
		rows = _rows("Task Overdue")
		self.assertEqual(len(rows), 1)
		self.assertIn("/crm/tasks/view/list?filters=", rows[0].link, "a task with no record opens the list")

	def test_a_done_or_canceled_task_is_never_swept(self):
		"""crm spells it `Canceled`, one L; a tuple saying `Cancelled` matched nothing and chased a cancelled task."""
		_gates(True)
		for status in ("Done", "Canceled"):
			frappe.db.set_value("CRM Task", self._task(add_to_date(now_datetime(), minutes=-60)).name, "status", status)
		events.sweep_reminders()
		self.assertEqual(_rows("Task Overdue"), [])

	def test_an_overdue_task_older_than_the_floor_is_left_alone(self):
		"""Without a floor the first pass after the switch is armed announces the whole backlog."""
		_gates(True)
		self._task(add_to_date(now_datetime(), days=-90))  # older than the 7-day default floor
		events.sweep_reminders()
		self.assertEqual(_rows("Task Overdue"), [])


class TestReminderOptionsReadFromTheSchema(unittest.TestCase):
	def test_every_option_the_settings_declare_reads_as_minutes(self):
		"""A new option the parser cannot read would silently fall back, so the doctype's own options are the cases."""
		options = api.lead_options()
		self.assertTrue(options and all(api.minutes(o) for o in options), options)
		self.assertIn(api.LEAD_DEFAULT_MINUTES, [api.minutes(o) for o in options], "the hour a rep gets by default is one they can pick")
		self.assertEqual((api.minutes("1 hour"), api.minutes("24 hours"), api.minutes("7 days")), (60, 1440, 10080))
