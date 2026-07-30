# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A TIMER GETS AN ALARM. THE DIARY AND THE SWEEP ARE UNCHANGED.

`wakeups.py:3` already states the principle: the durable Journey row is the truth, every enqueue is a
latency optimisation. Signals have had their accelerator since W1.2 — `deliver_signal` enqueues a resume
the moment the receipt lands. Timers never had one. So `Wait 2 minutes` meant 2 to 17 minutes, because
the only thing that would ever wake it was the */15 sweep. The node said one thing and did another.

THIS ADDS THE MISSING LEG AND NOTHING ELSE. The row is still written first and is still authoritative;
the sweep is still a complete standalone backstop and is still the volume path and still the only cover
for the ~61s window after a scheduler death that the W4 spike measured. It stops being load-bearing for
LATENCY. If a change here makes the sweep removable, §5.3/§5.4 have been misread.

WHY `enqueue_at` AND NOT `frappe.enqueue`. Frappe's signature has no delay parameter, and it hard-disables
RQ's scheduler on BOTH worker paths (`background_jobs.py:359` and `:364-366`) to avoid running two
schedulers — not to reject the capability. A queue Frappe's scheduler does not manage is an empty lane. We
put a scheduler in that lane and hand RQ the identical job `enqueue_call` already builds.

NOTHING IN REDIS IS A FACT. The payload is a journey NAME. The worker re-reads the row and re-checks status,
so a stale, duplicated or vanished job still does the right thing — runs go LATE, never WRONG.
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.workflow_engine import wakeups

_QUEUE = "workflow"


class TestTheAlarmIsSetOnTheEmptyLane(FrappeTestCase):
	"""THE red: there was no way to schedule a timer wake at all."""

	def _registry(self):
		from frappe.utils.background_jobs import get_queue
		from rq.registry import ScheduledJobRegistry

		return ScheduledJobRegistry(queue=get_queue(_QUEUE))

	def setUp(self):
		self.journey_name = f"RUN-TIMER-{frappe.generate_hash(length=8)}"
		self.addCleanup(self._forget)

	def _forget(self):
		registry = self._registry()
		for job_id in registry.get_job_ids():
			if self.journey_name in job_id:
				registry.remove(job_id, delete_job=True)

	def test_it_schedules_a_wake_on_the_workflow_queue(self):
		due = frappe.utils.add_to_date(frappe.utils.now_datetime(), minutes=2)

		wakeups.schedule_wake(self.journey_name, due)
		frappe.db.commit()

		scheduled = [j for j in self._registry().get_job_ids() if self.journey_name in j]
		self.assertTrue(scheduled, "no alarm was set — only the */15 sweep would wake this journey")

	def test_the_payload_is_a_name_never_the_row(self):
		"""§6.2 — every workflow entry in Redis is a pointer, never a fact."""
		from frappe.utils.background_jobs import get_redis_conn
		from rq.job import Job

		due = frappe.utils.add_to_date(frappe.utils.now_datetime(), minutes=2)
		wakeups.schedule_wake(self.journey_name, due)
		frappe.db.commit()

		job_id = next(j for j in self._registry().get_job_ids() if self.journey_name in j)
		job = Job.fetch(job_id, connection=get_redis_conn())

		self.assertEqual(job.kwargs["method"], "tatva_connect.workflow_engine.wakeups.drive_journey")
		self.assertEqual(job.kwargs["kwargs"], {"name": self.journey_name})

	def test_it_is_due_at_the_deadline_the_row_carries(self):
		"""The alarm and the diary must agree, or the accelerator wakes the journey at confidently the wrong
		time — worse than not waking it, because the sweep would at least have been honest.

		RQ keeps scheduled times in UTC; `resume_at` is written in the site's timezone. Compared as an
		offset from now in each frame, so the assertion cannot pass by both being wrong the same way.
		"""
		from datetime import datetime, timezone

		due = frappe.utils.add_to_date(frappe.utils.now_datetime(), minutes=2)

		wakeups.schedule_wake(self.journey_name, due)
		frappe.db.commit()

		job_id = next(j for j in self._registry().get_job_ids() if self.journey_name in j)
		scheduled_for = self._registry().get_scheduled_time(job_id)
		if scheduled_for.tzinfo is None:
			scheduled_for = scheduled_for.replace(tzinfo=timezone.utc)

		seconds_out = (scheduled_for - datetime.now(timezone.utc)).total_seconds()
		self.assertAlmostEqual(seconds_out, 120, delta=15, msg="the alarm disagrees with the row's deadline")

	def test_nothing_is_scheduled_until_the_segment_commits(self):
		"""enqueue_after_commit, kept: a rolled-back segment must schedule nothing. The row and the alarm
		are written in one transaction or neither."""
		due = frappe.utils.add_to_date(frappe.utils.now_datetime(), minutes=2)

		wakeups.schedule_wake(self.journey_name, due)
		before_commit = [j for j in self._registry().get_job_ids() if self.journey_name in j]
		frappe.db.rollback()
		after_rollback = [j for j in self._registry().get_job_ids() if self.journey_name in j]

		self.assertEqual(before_commit, [], "the alarm was set before the segment committed")
		self.assertEqual(after_rollback, [], "a rolled-back segment left an alarm behind")

	def test_a_second_alarm_for_one_run_does_not_stack(self):
		"""A re-park or a re-drive must not leave two alarms for one journey. Both would be no-ops, but the
		registry is not a bin."""
		due = frappe.utils.add_to_date(frappe.utils.now_datetime(), minutes=2)

		wakeups.schedule_wake(self.journey_name, due)
		wakeups.schedule_wake(self.journey_name, due)
		frappe.db.commit()

		scheduled = [j for j in self._registry().get_job_ids() if self.journey_name in j]
		self.assertEqual(len(scheduled), 1, f"{len(scheduled)} alarms for one journey")

	def test_a_deadline_already_past_is_still_scheduled_rather_than_dropped(self):
		"""An overdue park (a long segment, a clock skew) must still be woken by the alarm; dropping it
		would silently hand the journey back to the */15 sweep, which is the defect this closes."""
		due = frappe.utils.add_to_date(frappe.utils.now_datetime(), minutes=-5)

		wakeups.schedule_wake(self.journey_name, due)
		frappe.db.commit()

		self.assertTrue([j for j in self._registry().get_job_ids() if self.journey_name in j])
