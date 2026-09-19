# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""W14 — the workflow drain: one paced pass over parked journeys, due cohorts and buffered signals.

Holds: one booking however many parks, pulled forward and never pushed back; a due journey wakes; a backlog is
worked at the site pace, oldest first, and the pass books its successor; one pass at a time; the backstop books a
pass when work is waiting; a buffered signal behind unclaimable ones is still re-driven. Nothing here sends.
"""
import time
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect import utils
from tatva_connect.tatva_connect.doctype.crm_cohort_pace_settings import crm_cohort_pace_settings as pace
from tatva_connect.utils import book_drain
from tatva_connect.workflow_engine import drain, interpreter, signals, thresholds, wakeups
from tatva_connect.workflow_engine.tests import fixtures as fx

_WF = "ZZ Workflow Drain"
_SIGNAL = "probe.signal"


def _book(minutes):
	drain.pull_forward(frappe.utils.add_to_date(frappe.utils.now_datetime(), minutes=minutes))
	frappe.db.commit()


class TestAPassIsBookedOnce(FrappeTestCase):
	def setUp(self):
		fx.forget_pass()
		self.addCleanup(fx.forget_pass)

	def test_a_park_books_the_pass_at_its_deadline(self):
		_book(30)

		self.assertIsNotNone(fx.pass_booked_at(), "a park booked no pass, so only the backstop would wake it")
		self.assertAlmostEqual(fx.seconds_until(fx.pass_booked_at()), 1800, delta=15)

	def test_the_booking_names_the_pass_and_no_journey(self):
		from frappe.utils.background_jobs import get_redis_conn
		from rq.job import Job

		_book(30)
		(job_id,) = fx.pass_bookings()
		job = Job.fetch(job_id, connection=get_redis_conn())

		self.assertEqual(job.kwargs["method"], "tatva_connect.workflow_engine.drain.run")
		self.assertEqual(job.kwargs["kwargs"], {})

	def test_a_re_book_takes_a_new_job_id_so_a_running_pass_never_overwrites_its_own_record(self):
		_book(30)
		(first,) = fx.pass_bookings()
		wakeups.schedule_on_lane(frappe.utils.add_to_date(frappe.utils.now_datetime(), minutes=45), drain._PASS, {}, drain.DRAIN_KEY)
		frappe.db.commit()

		(second,) = fx.pass_bookings()
		self.assertNotEqual(first, second, "the successor reused the booking id of the pass that booked it")

	def test_a_later_deadline_never_pushes_the_pass_back(self):
		_book(30)
		_book(90)

		self.assertAlmostEqual(fx.seconds_until(fx.pass_booked_at()), 1800, delta=15)

	def test_an_earlier_deadline_pulls_the_pass_forward(self):
		_book(90)
		_book(30)

		self.assertAlmostEqual(fx.seconds_until(fx.pass_booked_at()), 1800, delta=15)

	def test_many_parks_hold_one_booking(self):
		from frappe.utils.background_jobs import get_queue
		from rq.registry import ScheduledJobRegistry

		registry = ScheduledJobRegistry(queue=get_queue(wakeups.WAKE_QUEUE))
		before = registry.count
		for minutes in range(30, 80):
			_book(minutes)

		self.assertEqual(registry.count - before, 1, "each park added a booking of its own")

	def test_nothing_is_booked_until_the_segment_commits(self):
		drain.pull_forward(frappe.utils.add_to_date(frappe.utils.now_datetime(), minutes=30))
		frappe.db.rollback()

		self.assertIsNone(fx.pass_booked_at(), "a rolled-back park left a booking behind")


class _DrainCase(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		fx.purge(_WF)
		cls.addClassCleanup(fx.purge, _WF)
		fx.arm_engine(True, cls=cls)
		# Published, not Active: the probe leads' own inserts must not start journeys through the trigger.
		cls.workflow = fx.make_workflow(_WF, [
			fx.trigger(to="w1"),
			fx.node("w1", "Wait", config={"mode": "For Duration", "duration": "{'minutes': 5}"}, edges={"next": "end"}),
			fx.node("end", "Terminal"),
		], lifecycle_state="Published")
		frappe.db.commit()

	def setUp(self):
		fx.forget_pass()
		self.addCleanup(fx.forget_pass)
		self.addCleanup(self._purge)
		self.leads = []

	def _purge(self):
		for name in frappe.get_all(fx.JOURNEY_DT, filters={"workflow": _WF}, pluck="name"):
			frappe.db.delete(fx.STEP_LOG_DT, {"journey": name})
			frappe.db.delete(fx.JOURNEY_DT, {"name": name})
		for lead in self.leads:
			frappe.db.delete(fx.SIGNAL_DT, {"subject_name": lead})
			frappe.delete_doc("CRM Lead", lead, force=True, ignore_permissions=True)
		frappe.db.commit()

	def _lead(self):
		lead = fx.make_lead().name
		self.leads.append(lead)
		return lead

	def _parked(self, due_in_minutes):
		"""A journey really parked at the Wait, its deadline moved to where the test needs it."""
		journey = fx.start_journey(self.workflow, self._lead(), "w1")
		interpreter.advance(frappe.get_doc(fx.JOURNEY_DT, journey.name))
		frappe.db.set_value(fx.JOURNEY_DT, journey.name, "resume_at",
		                    frappe.utils.add_to_date(frappe.utils.now_datetime(), minutes=due_in_minutes), update_modified=False)
		frappe.db.commit()
		fx.forget_pass()
		return journey.name

	def _status(self, name):
		return frappe.db.get_value(fx.JOURNEY_DT, name, "status")

	def _signal_row(self, lead, correlation):
		frappe.get_doc({
			"doctype": fx.SIGNAL_DT, "subject_doctype": "CRM Lead", "subject_name": lead,
			"event_name": _SIGNAL, "correlation": correlation, "payload_json": "{}", "status": "Pending",
		}).insert(ignore_permissions=True)  # authz-ok: tier-c — test fixture, no user input
		frappe.db.commit()


class TestAPassWakesDueJourneys(_DrainCase):
	def test_a_due_journey_wakes(self):
		name = self._parked(-1)

		drain.run()

		self.assertEqual(self._status(name), "Done")

	def test_a_journey_not_yet_due_stays_parked_and_the_next_pass_is_booked_for_it(self):
		name = self._parked(45)

		drain.run()

		self.assertEqual(self._status(name), "Parked")
		self.assertIsNotNone(fx.pass_booked_at(), "an idle pass booked nothing")
		self.assertAlmostEqual(fx.seconds_until(fx.pass_booked_at()), 45 * 60, delta=30)

	def test_a_backlog_is_worked_at_the_site_pace_oldest_first(self):
		older = self._parked(-3)
		newer = self._parked(-2)

		with patch.object(pace, "site_pace", return_value=(1, 30)):
			moved = drain.run()

		self.assertEqual(moved, 1, "the pass ignored the batch size")
		self.assertEqual(self._status(older), "Done", "the oldest deadline was not woken first")
		self.assertEqual(self._status(newer), "Parked")
		self.assertAlmostEqual(fx.seconds_until(fx.pass_booked_at()), 30, delta=10, msg="the successor ignored the pace")

	def test_a_deep_lane_is_left_to_empty(self):
		name = self._parked(-1)

		with patch.object(utils, "lane_depth", return_value=utils.lane_busy_at()):
			moved = drain.run()

		self.assertEqual(moved, 0)
		self.assertEqual(self._status(name), "Parked", "a pass fed a lane already deep in jobs")
		self.assertIsNotNone(fx.pass_booked_at(), "a pass that held back booked no successor")

	def test_an_early_drive_keeps_the_deadline_the_wait_started_with(self):
		name = self._parked(45)
		deadline = frappe.db.get_value(fx.JOURNEY_DT, name, "resume_at")

		wakeups.drive_journey(name)

		self.assertEqual(self._status(name), "Parked")
		self.assertEqual(frappe.db.get_value(fx.JOURNEY_DT, name, "resume_at"), deadline, "an early drive pushed the timer back")

	def test_the_busy_line_follows_frappes_own_ceiling_and_stops_at_a_real_backlog(self):
		"""A site that raises frappe's ceiling moves the line with it; the cap is what a backlog means either way."""
		for ceiling, expected in ((500, 200), (2000, 200), (300, 120)):
			with self.subTest(ceiling=ceiling), patch.dict(frappe.conf, {"max_queued_jobs": ceiling}, clear=False):
				self.assertEqual(utils.lane_busy_at(), expected)

	def test_a_disarmed_engine_moves_nothing(self):
		name = self._parked(-1)
		fx.arm_engine(False)
		try:
			drain.run()
		finally:
			fx.arm_engine(True)

		self.assertEqual(self._status(name), "Parked")


class TestOnePassAtATime(_DrainCase):
	def test_a_pass_that_finds_the_lock_taken_works_nothing_and_books_a_retry(self):
		name = self._parked(-1)
		self.assertTrue(book_drain(drain.DRAIN_KEY, thresholds.WORKFLOW_DRAIN_LEASE_SECONDS))

		with patch.object(pace, "site_pace", return_value=(60, 30)):
			self.assertEqual(drain.run(), 0)

		self.assertEqual(self._status(name), "Parked", "a second pass worked what another pass holds")
		self.assertAlmostEqual(fx.seconds_until(fx.pass_booked_at()), 30, delta=10, msg="the losing pass booked no retry")

	def test_a_pass_renews_its_lease_while_it_works(self):
		"""A short lease is only safe because a live pass keeps saying it is here; a dead one stops and the pile frees."""
		self._parked(-1)
		with patch("tatva_connect.workflow_engine.drain.hold_drain") as held:
			drain.run()

		self.assertTrue(held.called, "the pass never renewed its lease, so a long pass would lose the pile")
		self.assertEqual(held.call_args.args, (drain.DRAIN_KEY, thresholds.WORKFLOW_DRAIN_LEASE_SECONDS))

	def test_a_kicked_pass_is_deduplicated_on_the_lane(self):
		with patch("frappe.enqueue") as enqueue:
			drain.kick()

		kwargs = enqueue.call_args.kwargs
		self.assertEqual((kwargs["job_id"], kwargs["deduplicate"], kwargs["enqueue_after_commit"]), (drain.DRAIN_KEY, True, True))



class TestTheBackstop(_DrainCase):
	def test_it_books_a_pass_when_a_journey_is_due(self):
		name = self._parked(-1)

		wakeups.sweep()

		self.assertEqual(self._status(name), "Done", "the backstop found a due journey and no pass woke it")

	def test_a_signal_no_parked_journey_can_take_is_not_work(self):
		self._signal_row(self._lead(), None)

		self.assertEqual(signals.claimable(limit=1), [], "an unclaimable signal made the backstop queue a pass")

	def test_it_books_nothing_when_nothing_waits(self):
		with patch.object(drain, "has_work", return_value=False), patch.object(drain, "kick") as kick:
			wakeups.sweep()

		kick.assert_not_called()


class TestAPassRedrivesBufferedSignals(_DrainCase):

	def test_a_journey_behind_a_page_of_unclaimable_signals_is_still_redriven(self):
		for _ in range(3):
			self._signal_row(self._lead(), None)
		lead = self._lead()
		journey = fx.start_journey(self.workflow, lead, "end")
		correlation = f"{journey.name}::w1"
		frappe.db.set_value(fx.JOURNEY_DT, journey.name, {
			"status": "Parked", "awaiting_signal": _SIGNAL, "awaiting_correlation": correlation,
		}, update_modified=False)
		frappe.db.commit()
		self._signal_row(lead, correlation)

		with patch.object(thresholds, "SWEEP_PAGE", 1):
			signals.redrive(10, time.monotonic() + 60, lambda: None)

		self.assertEqual(self._status(journey.name), "Done", "the re-drive stopped at the first page")
