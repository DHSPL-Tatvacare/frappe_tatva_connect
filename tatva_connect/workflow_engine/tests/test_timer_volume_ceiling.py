# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""W4 — the timer path hands over to the sweep at volume, and the alarm stops being set.

EVERY WORKFLOW ENTRY IN REDIS IS A POINTER OR A COPY, NEVER A FACT (§6.2). An alarm is a copy of
`resume_at`, which is already the truth, held for the WHOLE wait — a six-month journey holds one for six
months. §5.4 declares the sweep is the volume path, and until now it was not: every park set an alarm
however many were already pending.

So above `SCHEDULE_TO_DRAIN_HANDOVER` the alarm is not set. The row still carries `resume_at`, the sweep
still finds it, and the journey goes LATE, NEVER WRONG — §6.2's accepted failure, with the reverse fatal.

NOT the reason, and struck from scope: a burst past `MAX_QUEUED_JOBS`. `schedule_wake` uses the raw RQ
`queue.enqueue_at` and never enters `frappe.enqueue`, and `_check_queue_size` reads the READY queue, not
the scheduled registry. Measured at 600 alarms: `q.count=0`, `registry.count=600`, no throw. A test for
that throw would pass for the wrong reason.

THE CEILING IS DRIVEN AGAINST THE REAL REGISTRY. `SCHEDULE_TO_DRAIN_HANDOVER` is patched RELATIVE to the
live count rather than the registry being stuffed to 200 — the ceiling is what is under test, not RQ's
ability to hold rows, and a bench whose registry is not empty must not change the answer.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \\
        --module tatva_connect.workflow_engine.tests.test_timer_volume_ceiling
"""
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.workflow_engine import interpreter, thresholds, wakeups
from tatva_connect.workflow_engine.tests import fixtures as fx

_WF = "ZZ Timer Ceiling"


def _registry():
	from frappe.utils.background_jobs import get_queue
	from rq.registry import ScheduledJobRegistry

	return ScheduledJobRegistry(queue=get_queue(wakeups.WAKE_QUEUE))


def _job_id(journey_name):
	from frappe.utils.background_jobs import create_job_id

	return create_job_id(f"workflow-wake::{journey_name}")


class _CeilingCase(FrappeTestCase):
	"""Every alarm this suite writes is removed by name in `addCleanup`, registered BEFORE it can exist —
	a probe key left in Redis outlives the test that made it and is invisible to the next reader."""

	def setUp(self):
		self.mine = []
		self.addCleanup(self._forget_mine)

	def _forget_mine(self):
		registry = _registry()
		for name in self.mine:
			job_id = _job_id(name)
			if job_id in registry.get_job_ids():
				registry.remove(job_id, delete_job=True)

	def _name(self):
		name = f"ZZCEIL-{frappe.generate_hash(length=8)}"
		self.mine.append(name)
		return name

	def _schedule(self, name, minutes=30):
		"""One park's worth of alarm, committed — `schedule_wake` registers an after-commit hook."""
		due = frappe.utils.add_to_date(frappe.utils.now_datetime(), minutes=minutes)
		set_it = wakeups.schedule_wake(name, due)
		frappe.db.commit()
		return set_it

	def _ceiling_at(self, offset):
		"""Patch the declared handover RELATIVE to what is really pending, so a dirty bench cannot flip
		the answer. `offset=0` puts us exactly at the ceiling; a positive offset leaves room."""
		return patch.object(thresholds, "SCHEDULE_TO_DRAIN_HANDOVER", _registry().count + offset)


class TestTheCeilingDecidesWhetherAnAlarmIsSet(_CeilingCase):
	def test_below_the_ceiling_every_park_still_sets_its_alarm(self):
		"""The half that must not regress: punctuality is the whole point of the timer accelerator."""
		name = self._name()

		with self._ceiling_at(5):
			set_it = self._schedule(name)

		self.assertTrue(set_it, "schedule_wake reported no alarm below the ceiling")
		self.assertIn(_job_id(name), _registry().get_job_ids(), "the alarm was not written to the lane")

	def test_at_the_ceiling_the_alarm_is_not_set(self):
		"""The registry stops growing. This is the outcome — not that a guard function was called."""
		name = self._name()
		before = _registry().count

		with self._ceiling_at(0):
			set_it = self._schedule(name)

		self.assertFalse(set_it, "schedule_wake claimed an alarm it did not set")
		self.assertNotIn(_job_id(name), _registry().get_job_ids(), "an alarm was set above the ceiling")
		self.assertEqual(_registry().count, before, "the scheduled registry grew above the ceiling")

	def test_a_re_park_above_the_ceiling_drops_the_alarm_it_already_had(self):
		"""THE trap in skipping the whole block. `drive_journey` claims on status ALONE and never re-reads
		the clock, so an alarm left over from an earlier park does not go stale — it wakes the journey
		EARLY, at the old instant, which is the one failure this design is not allowed to have."""
		name = self._name()
		with self._ceiling_at(5):
			self._schedule(name, minutes=1)
		self.assertIn(_job_id(name), _registry().get_job_ids(), "the fixture must start with an alarm")

		with self._ceiling_at(0):
			self._schedule(name, minutes=90)

		self.assertNotIn(
			_job_id(name), _registry().get_job_ids(),
			"the earlier alarm survived, so this journey wakes at the instant it was FIRST parked for",
		)

	def test_a_re_park_replaces_its_alarm_rather_than_stacking_one(self):
		"""What `_forget_wake` is for, asserted as an outcome — one journey, one alarm, however often it
		re-parks. The native membership test is the implementation; this is the behaviour it must keep."""
		name = self._name()

		with self._ceiling_at(5):
			self._schedule(name, minutes=1)
			self._schedule(name, minutes=90)

		held = [j for j in _registry().get_job_ids() if j == _job_id(name)]
		self.assertEqual(len(held), 1, "a re-park stacked a second alarm for one journey")


class TestTheJourneyStillWakesAndSaysWhy(_CeilingCase):
	"""The diary is untouched by the ceiling, so the sweep is a complete standalone backstop — driven for
	real, because a status column proves nothing about what actually wakes a journey."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		fx.purge(_WF)
		cls.addClassCleanup(fx.purge, _WF)
		fx.arm_engine(True, cls=cls)
		# `timer_sweep` is gated on the sweep switch, and this suite drives the real one.
		fx.arm_sweep(cls)
		cls.workflow = fx.make_workflow(_WF, [
			fx.trigger(to="w1"),
			fx.node("w1", "Wait", config={"mode": "For Duration", "duration": "{'minutes': 5}"},
			        edges={"next": "end"}),
			fx.node("end", "Terminal"),
		])

	def setUp(self):
		super().setUp()
		self.lead = fx.make_lead()
		self.addCleanup(self._drop_lead, self.lead.name)
		self.journey = fx.start_journey(self.workflow, self.lead.name, "w1")
		self.mine.append(self.journey.name)
		self.addCleanup(self._purge_journey)

	def _drop_lead(self, name):
		if frappe.db.exists("CRM Lead", name):
			frappe.delete_doc("CRM Lead", name, force=True, ignore_permissions=True)
			frappe.db.commit()

	def _purge_journey(self):
		frappe.db.delete(fx.STEP_LOG_DT, {"journey": self.journey.name})
		frappe.db.delete(fx.JOURNEY_DT, {"name": self.journey.name})
		frappe.db.commit()

	def _park_above_the_ceiling(self):
		with self._ceiling_at(0):
			interpreter.advance(frappe.get_doc(fx.JOURNEY_DT, self.journey.name))
			frappe.db.commit()

	def test_the_diary_row_is_written_even_though_no_alarm_was(self):
		"""`resume_at` is the truth; the alarm was only ever a copy of it."""
		self._park_above_the_ceiling()

		row = frappe.db.get_value(fx.JOURNEY_DT, self.journey.name, ["status", "resume_at"], as_dict=True)
		self.assertEqual(row.status, "Parked")
		self.assertTrue(row.resume_at, "a park above the ceiling wrote no deadline, so nothing can wake it")
		self.assertNotIn(_job_id(self.journey.name), _registry().get_job_ids())

	def test_the_sweep_still_wakes_it(self):
		"""The real `timer_sweep`, against a real due row — the backstop doing the work the alarm did."""
		self._park_above_the_ceiling()
		frappe.db.set_value(
			fx.JOURNEY_DT, self.journey.name, "resume_at",
			frappe.utils.add_to_date(frappe.utils.now_datetime(), minutes=-1), update_modified=False,
		)
		frappe.db.commit()

		wakeups.timer_sweep()

		self.assertEqual(
			frappe.db.get_value(fx.JOURNEY_DT, self.journey.name, "status"), "Done",
			"a journey parked above the ceiling was never woken by the sweep",
		)

	def test_the_parked_step_says_the_alarm_was_not_set(self):
		"""THE observability requirement, and it costs nothing: the audit row this park already writes is
		where an operator asks "why did this wait 12 minutes instead of 2". A log line per park would be
		noise at exactly the volume that triggers this, and answers about the fleet, not about this patient."""
		self._park_above_the_ceiling()

		parked = [s for s in fx.logs(self.journey.name) if s.outcome == "parked"]
		self.assertTrue(parked, "the park wrote no step at all")
		self.assertIn("sweep", (parked[-1].detail or "").lower(),
		              f"the parked step does not say the wake was handed to the sweep: {parked[-1].detail!r}")
