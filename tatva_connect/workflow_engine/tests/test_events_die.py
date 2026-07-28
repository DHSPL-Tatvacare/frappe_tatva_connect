# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""W4.4 — NOTHING ACCUMULATES FOR EVER. Every event reaches a terminal state, and a dead one is inert.

THE RED. `CRM Workflow Event` had two states and only one way out: a Wait consuming the row. A signal
nothing ever claimed — an early delivery for a run that died before it parked, a duplicate, a receipt for
a workflow since archived — sat `Pending` for a month and was then DELETED. Seventeen were measured on
this bench spanning a whole day. Two defects in one shape: the row reached no terminal state, so the inbox
could never say what became of it; and for the whole month it stayed live it could still be claimed by a
park, which is a message leaving on a weeks-old signal the moment sends are armed.

WHAT THIS LOCKS. Expiry is a STATE, not a delete. `Expired` is terminal and inert, because
`pending_signal_filters` — the ONE description of "a row that would wake this park" — asks for `PENDING`
and nothing else. No waking surface has to remember an age check, which is why the backstop was made to
ask through that same filter rather than keep the private copy it used to carry.
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.workflow_engine import interpreter, thresholds, wakeups
from tatva_connect.workflow_engine.tests import fixtures

SIGNAL_DT = interpreter.SIGNAL_DT


def _event(status=interpreter.PENDING, age_days=0, correlation=None, subject=None):
	"""One inbox row, aged by writing `creation` directly — the column the reaper judges on.

	`subject_name` is a Dynamic Link, so the lead has to be REAL — a made-up name fails validation, which
	is the doctype refusing to hold an event about a record that does not exist.
	"""
	row = frappe.get_doc({
		"doctype": SIGNAL_DT,
		"subject_doctype": "CRM Lead",
		"subject_name": subject or fixtures.make_lead().name,
		"event_name": "probe.signal",
		"correlation": correlation,
		"payload_json": "{}",
		"status": status,
	}).insert(ignore_permissions=True)
	if age_days:
		old = frappe.utils.add_to_date(frappe.utils.now_datetime(), days=-age_days)
		frappe.db.set_value(SIGNAL_DT, row.name, {"creation": old, "modified": old}, update_modified=False)
	return row.name


class TestEveryEventReachesATerminalState(FrappeTestCase):
	def tearDown(self):
		frappe.db.rollback()

	def test_an_unclaimed_event_is_expired_not_deleted(self):
		"""The whole defect: it used to vanish, so nothing could say what became of it."""
		name = _event(age_days=thresholds.SIGNAL_DEAD_AFTER_DAYS + 1)

		wakeups._purge_stale_signals()

		self.assertTrue(frappe.db.exists(SIGNAL_DT, name), "the row must survive its own expiry")
		self.assertEqual(frappe.db.get_value(SIGNAL_DT, name, "status"), interpreter.EXPIRED)

	def test_a_live_event_is_left_alone(self):
		"""Early delivery is a guarantee this engine makes; the reaper must not break it."""
		name = _event(age_days=thresholds.SIGNAL_DEAD_AFTER_DAYS - 1)

		wakeups._purge_stale_signals()

		self.assertEqual(frappe.db.get_value(SIGNAL_DT, name, "status"), interpreter.PENDING)

	def test_terminal_rows_are_deleted_after_the_retention_window(self):
		"""Bounded, both ways out: consumed and expired age out on the same declared window."""
		consumed = _event(status=interpreter.CONSUMED, age_days=thresholds.SIGNAL_RETENTION_DAYS + 1)
		expired = _event(status=interpreter.EXPIRED, age_days=thresholds.SIGNAL_RETENTION_DAYS + 1)
		kept = _event(status=interpreter.CONSUMED, age_days=thresholds.SIGNAL_RETENTION_DAYS - 1)

		wakeups._purge_stale_signals()

		self.assertFalse(frappe.db.exists(SIGNAL_DT, consumed))
		self.assertFalse(frappe.db.exists(SIGNAL_DT, expired), "an expired row is terminal like any other")
		self.assertTrue(frappe.db.exists(SIGNAL_DT, kept))

	def test_expiry_stamps_modified_so_it_can_itself_be_aged_out(self):
		"""An expiry with no timestamp would be immortal — the leak, one state further along."""
		name = _event(age_days=thresholds.SIGNAL_DEAD_AFTER_DAYS + 1)
		before = frappe.db.get_value(SIGNAL_DT, name, "modified")

		wakeups._purge_stale_signals()

		self.assertGreater(frappe.db.get_value(SIGNAL_DT, name, "modified"), before)


class TestAStaleEventCannotWakeAJourney(FrappeTestCase):
	"""The comms risk, not the housekeeping one."""

	def tearDown(self):
		frappe.db.rollback()

	def test_an_expired_event_is_invisible_to_the_one_waking_filter(self):
		subject = fixtures.make_lead().name
		name = _event(status=interpreter.EXPIRED, subject=subject)

		filters = interpreter.pending_signal_filters("CRM Lead", subject, "probe.signal", None)

		self.assertIsNone(frappe.db.get_value(SIGNAL_DT, filters, "name"))
		self.assertTrue(frappe.db.exists(SIGNAL_DT, name), "inert, but still on the record")

	def test_a_pending_event_still_is(self):
		"""The guard has to be the STATE, not the query — this fails if expiry over-reached."""
		subject = fixtures.make_lead().name
		name = _event(subject=subject)

		filters = interpreter.pending_signal_filters("CRM Lead", subject, "probe.signal", None)

		self.assertEqual(frappe.db.get_value(SIGNAL_DT, filters, "name"), name)

	def test_the_backstop_asks_through_the_same_filter_it_would_consume_by(self):
		"""It carried a private copy of this dict, which is exactly what would let an Expired row wake a run."""
		subject = fixtures.make_lead().name
		_event(status=interpreter.EXPIRED, subject=subject)
		row = frappe._dict(
			subject_doctype="CRM Lead", subject_name=subject,
			awaiting_signal="probe.signal", awaiting_correlation=None,
		)

		self.assertFalse(wakeups._has_pending_signal(row))
