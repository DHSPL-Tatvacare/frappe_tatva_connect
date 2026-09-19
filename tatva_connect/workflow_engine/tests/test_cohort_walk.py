# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""W14 — a scheduled cohort is walked by the workflow drain, a batch per pass, and a dead pass loses nothing.

Holds: every matching lead once and no decoy; a pass killed right after a start resumes past that lead; a time
slice stops a batch and the walk carries on from there; an abort ends the walk and leaves its journeys; the drain
starts a due cohort at the site pace; activation books the first run. The graph is Trigger -> Terminal, so no
unique key hides a double start. Nothing here sends.
"""
import itertools
import time
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.tatva_connect.doctype.crm_cohort_pace_settings import crm_cohort_pace_settings as pace
from tatva_connect.workflow_engine import cohort, drain, registry, triggers, wakeups
from tatva_connect.workflow_engine.tests import fixtures as fx

_LEADS = 7
_DECOYS = 3


class _Killed(BaseException):
	"""A worker dying mid-pass: not an `Exception`, so nothing in the walk can catch it and carry on."""


class _WalkCase(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.workflow_name = f"ZZ Cohort Walk {cls.__name__}"
		cls.marker = f"cohort-walk-{cls.__name__}"
		fx.purge(cls.workflow_name)
		cls.addClassCleanup(fx.purge, cls.workflow_name)
		fx.arm_engine(True, cls=cls)
		# The marker rides `custom_external_id`, a label nothing else on this grain writes, set AT INSERT.
		cls.leads = [fx.make_lead(custom_external_id=cls.marker).name for _ in range(_LEADS)]
		cls.decoys = [fx.make_lead(custom_external_id=f"{cls.marker}-decoy").name for _ in range(_DECOYS)]
		cls.addClassCleanup(cls._drop_leads)
		cls.workflow = fx.make_workflow(cls.workflow_name, [
			fx.node("start", "Trigger", config={
				"mode": registry.MODE_SCHEDULE, "subject_doctype": "CRM Lead",
				"schedule": "Daily", "schedule_time": "09:00",
				"vertical": fx.GRAIN["vertical"], "group": fx.GRAIN["group"], "program": fx.GRAIN["program"],
				"predicate": {"type": "rule", "field": "crm_lead.custom_external_id", "operator": "is",
				              "value": cls.marker},
			}, edges={"next": "end"}),
			fx.node("end", "Terminal"),
		])
		frappe.db.commit()

	@classmethod
	def _drop_leads(cls):
		for lead in cls.leads + cls.decoys:
			frappe.delete_doc("CRM Lead", lead, force=True, ignore_permissions=True)
		frappe.db.commit()

	def setUp(self):
		self._reset()
		self.addCleanup(self._reset)
		fx.forget_pass()
		self.addCleanup(fx.forget_pass)

	def _reset(self):
		for run in frappe.get_all(fx.JOURNEY_DT, filters={"workflow": self.workflow_name}, pluck="name"):
			frappe.db.delete(fx.STEP_LOG_DT, {"journey": run})
		frappe.db.delete(fx.JOURNEY_DT, {"workflow": self.workflow_name})
		frappe.db.set_value(fx.WORKFLOW_DT, self.workflow_name, {
			"cohort_cursor": "", "cohort_abort": 0, "cohort_state": "",
			"trigger_next_run_at": frappe.utils.add_to_date(frappe.utils.now_datetime(), minutes=-1),
		}, update_modified=False)
		frappe.db.commit()

	def _started(self):
		return frappe.get_all(fx.JOURNEY_DT, filters={"workflow": self.workflow_name}, pluck="subject_name")

	def _row(self):
		return frappe.db.get_value(
			fx.WORKFLOW_DT, self.workflow_name,
			["cohort_state", "cohort_cursor", "cohort_abort", "trigger_next_run_at"], as_dict=True,
		)

	def _pass(self, limit=3):
		return cohort.start_due(limit, time.monotonic() + 60, lambda: None)

	def _walk_to_the_end(self, limit=3, passes=10):
		for _ in range(passes):
			if self.workflow_name not in cohort.due_workflows():
				return
			self._pass(limit)
		self.fail(f"the cohort was still due after {passes} passes")

	def _assert_each_lead_once(self):
		started = self._started()
		self.assertEqual(sorted(started), sorted(self.leads), "the walk did not start every matching lead exactly once")


class TestTheWalk(_WalkCase):
	def test_every_matching_lead_once_and_no_decoy(self):
		self._walk_to_the_end(limit=3)

		self._assert_each_lead_once()
		self.assertFalse(set(self._started()) & set(self.decoys), "a lead the criteria reject was started")

	def test_a_batch_stops_at_its_limit_and_the_walk_stays_due(self):
		taken = self._pass(limit=3)

		self.assertEqual(taken, 3)
		self.assertEqual(len(self._started()), 3)
		row = self._row()
		self.assertEqual(row.cohort_state, cohort.DRAINING, "a walk with leads left does not read as running")
		self.assertTrue(row.cohort_cursor, "a walk with leads left kept no cursor to resume from")
		self.assertIn(self.workflow_name, cohort.due_workflows(), "an unfinished walk fell out of the due list")

	def test_a_finished_walk_clears_itself_and_moves_the_clock(self):
		self._walk_to_the_end(limit=3)

		row = self._row()
		self.assertEqual(row.cohort_state, cohort.IDLE)
		self.assertFalse(row.cohort_cursor, "a finished walk left a cursor the next occurrence would start from")
		self.assertGreater(row.trigger_next_run_at, frappe.utils.now_datetime(), "a finished walk did not move its clock")

	def test_a_pass_killed_right_after_a_start_resumes_past_that_lead(self):
		"""THE duplicate-message guard. The start commits; the worker dies before anything else commits."""
		real = triggers.start_journey
		calls = itertools.count()

		def dies_after_the_first_start(*args, **kwargs):
			result = real(*args, **kwargs)
			if next(calls) == 0:
				raise _Killed()
			return result

		with patch.object(triggers, "start_journey", side_effect=dies_after_the_first_start):
			with self.assertRaises(_Killed):
				self._pass(limit=3)
		frappe.db.rollback()

		self._walk_to_the_end(limit=3)

		self._assert_each_lead_once()

	def test_a_time_slice_stops_a_batch_and_the_walk_carries_on_from_there(self):
		real, begun = triggers.start_journey, []

		def counted(*args, **kwargs):
			begun.append(args)
			return real(*args, **kwargs)

		# The clock runs out once two leads have begun — read off a counter, so nothing the clock is asked by can recurse into it.
		with patch.object(triggers, "start_journey", side_effect=counted), \
		     patch.object(cohort.time, "monotonic", side_effect=lambda: 100 if len(begun) >= 2 else 0):
			taken = cohort.start_due(5, 50, lambda: None)

		self.assertEqual(taken, 2, "the batch ignored the time slice")
		self.assertEqual(len(self._started()), 2)

		self._walk_to_the_end(limit=3)

		self._assert_each_lead_once()


class TestASuspendLandsWithinOneLead(_WalkCase):
	def test_a_suspend_mid_batch_stops_the_next_start(self):
		real = triggers.start_journey

		def suspends_after_the_first(*args, **kwargs):
			result = real(*args, **kwargs)
			frappe.db.set_value(fx.WORKFLOW_DT, self.workflow_name, "lifecycle_state", "Suspended", update_modified=False)
			frappe.db.commit()
			return result

		self.addCleanup(frappe.db.set_value, fx.WORKFLOW_DT, self.workflow_name, "lifecycle_state", "Active", update_modified=False)
		with patch.object(triggers, "start_journey", side_effect=suspends_after_the_first):
			self._pass(limit=5)

		self.assertEqual(len(self._started()), 1, "the batch kept starting leads after the workflow was suspended")


class TestTheAbort(_WalkCase):
	def test_an_abort_ends_an_active_walk_and_leaves_its_journeys(self):
		self._pass(limit=2)
		cohort.abort(self.workflow_name)

		self._pass(limit=3)

		self.assertEqual(len(self._started()), 2, "the walk kept starting leads after the abort")
		row = self._row()
		self.assertEqual(row.cohort_state, cohort.IDLE)
		self.assertFalse(row.cohort_abort, "the abort outlived the occurrence it ended")
		self.assertNotIn(self.workflow_name, cohort.due_workflows(), "an aborted walk is still due")


class TestTheOneDrainCarriesIt(_WalkCase):
	def test_the_drain_starts_a_due_cohort_at_the_site_pace(self):
		with patch.object(pace, "site_pace", return_value=(3, 30)), patch.object(wakeups, "due_journeys", return_value=[]):
			moved = drain.run()

		self.assertEqual(moved, 3)
		self.assertEqual(len(self._started()), 3, "the drain ignored the pace")
		self.assertIsNotNone(fx.pass_booked_at(), "a pass with a cohort still due booked no successor")
		self.assertAlmostEqual(fx.seconds_until(fx.pass_booked_at()), 30, delta=10)

	def test_a_disarmed_engine_starts_no_cohort(self):
		fx.arm_engine(False)
		try:
			drain.run()
		finally:
			fx.arm_engine(True)

		self.assertEqual(self._started(), [], "a disarmed engine started a cohort")

	def test_the_backstop_sees_a_due_cohort(self):
		with patch.object(wakeups, "due_journeys", return_value=[]):
			self.assertTrue(drain.has_work(), "a due cohort is invisible to the backstop")

	def test_activating_a_scheduled_workflow_books_the_drain_for_its_first_run(self):
		doc = frappe.get_doc(fx.WORKFLOW_DT, self.workflow_name)
		doc.apply_transition("Suspended")
		frappe.db.commit()
		fx.forget_pass()

		doc = frappe.get_doc(fx.WORKFLOW_DT, self.workflow_name)
		doc.apply_transition("Active")
		frappe.db.commit()

		first_run = wakeups._as_utc(frappe.utils.get_datetime(self._row().trigger_next_run_at))
		self.assertIsNotNone(fx.pass_booked_at(), "an activated scheduled workflow booked no pass for its first run")
		self.assertAlmostEqual(fx.seconds_until(fx.pass_booked_at()), fx.seconds_until(first_run), delta=5)
