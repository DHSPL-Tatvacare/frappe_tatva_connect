# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""W7.2 PART B — the drain that walks the cohort. ONE JOB, a keyset cursor, committed per chunk.

A COHORT IS A journey FACTORY. The schedule fires, the drain walks the leads its criteria select, and each
one gets its OWN ordinary journey down the identical graph through `triggers.start_journey` — the same entry the
record-event lane uses. Nothing about node contracts, park/resume or the interpreter changes.

THE FOUR THINGS THIS SUITE EXISTS TO HOLD:
  * ONE JOB, NEVER N. `frappe.enqueue` THROWS above `MAX_QUEUED_JOBS = 500`, so a 2,000-lead cohort that
    became 2,000 jobs would error partway and leave the rest of the cohort silently unstarted.
  * A RESUME CANNOT DOUBLE-START A LEAD. A worker killed mid-drain resumes from the stored cursor, and
    `_start_one`'s `active_key` UNIQUE index is the second line behind it.
  * THE ABORT STOPS IT BETWEEN CHUNKS, and leaves already-started runs alone — the product owner's call.
    A per-lead cancel is a different need and belongs with W10.
  * WITH THE SWITCH OFF, NOTHING IS BORN. The switch ships OFF and arms nothing by existing.

Nothing here sends. The engine switch is armed only inside the tests that need a journey to exist, and the
sends gate stays off throughout, so no node reaches a provider.
"""
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.workflow_engine import cohort, drain, registry
from tatva_connect.workflow_engine.tests import fixtures as fx

_LEADS = 7


class _CohortCase(FrappeTestCase):
	"""A scheduled workflow over a handful of leads on one grain.

	A test ABOUT THE WALK says `respect_switch=False` out loud, because `fx.arm_engine` arms
	`Workflow::Engine::run` and never `Workflow::Cohort::drain`, and the safe value is the default.
	"""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		# EACH CLASS OWNS ITS OWN COHORT. Sharing one marker and one workflow name across classes made the
		# suites read each other's leads — a cohort correctly takes everyone its criteria match, so the
		# criteria have to be what isolates them, not the teardown order.
		cls.workflow_name = f"cohort-drain-{cls.__name__}"
		cls.marker = f"cohort-marker-{cls.__name__}"
		fx.purge(cls.workflow_name)
		fx.arm_engine(True, cls)
		# The marker is set AT INSERT: a predicate reads through `frappe.get_doc`, so a bare column write
		# afterwards can be judged against a cached document that still says otherwise.
		cls.leads = [fx.make_lead(first_name=cls.marker) for _ in range(_LEADS)]
		cls.workflow = fx.make_workflow(cls.workflow_name, [
			fx.node("start", "Trigger", config={
				"mode": registry.MODE_SCHEDULE, "subject_doctype": "CRM Lead",
				"schedule": "Daily", "schedule_time": "09:00",
				"vertical": fx.GRAIN["vertical"], "group": fx.GRAIN["group"], "program": fx.GRAIN["program"],
				"predicate": {"type": "rule", "field": "crm_lead.first_name", "operator": "is",
				              "value": cls.marker},
			}, edges={"next": "w1"}),
			fx.node("w1", "Wait", config={"mode": registry.UNTIL_EVENT, "event_name": "task.completed"},
			        edges={"event": "end"}),
			fx.node("end", "Terminal"),
		])
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		fx.purge(cls.workflow_name)
		for lead in cls.leads:
			frappe.delete_doc("CRM Lead", lead.name, force=True, ignore_permissions=True)
		frappe.db.commit()
		super().tearDownClass()

	def setUp(self):
		self._reset()
		# PACING IS REAL AND ITS BUCKET IS SHARED — a global Redis budget that survives between runs, so a
		# suite run twice in a minute throttles itself and every cursor/abort assertion turns into a
		# pacing assertion. Tests about the WALK take tokens for granted; `TestPacingRidesTheExistingBucket`
		# is where the limiter itself is exercised, and it patches this the other way.
		token = patch.object(drain, "_take_token", return_value=True)
		token.start()
		self.addCleanup(token.stop)

	def tearDown(self):
		self._reset()

	def _reset(self):
		for run in frappe.get_all(fx.JOURNEY_DT, filters={"workflow": self.workflow_name}, pluck="name"):
			frappe.db.delete(fx.STEP_LOG_DT, {"journey": run})
		frappe.db.delete(fx.JOURNEY_DT, {"workflow": self.workflow_name})
		frappe.db.set_value("CRM Workflow", self.workflow_name, {
			"cohort_cursor": "", "cohort_abort": 0, "cohort_state": "",
		}, update_modified=False)
		frappe.db.commit()

	def _runs(self):
		return frappe.get_all(fx.JOURNEY_DT, filters={"workflow": self.workflow_name}, pluck="subject_name")


class TestTheDrainIsOneJobNotOnePerLead(_CohortCase):
	"""`frappe.enqueue` throws above MAX_QUEUED_JOBS=500. A cohort that enqueued per lead would error
	partway through and leave the rest of the cohort silently unstarted — which is the whole reason the
	settled answer is 'one drain job walking a keyset cursor'."""

	def test_the_whole_cohort_costs_exactly_one_enqueue(self):
		frappe.db.set_value("CRM Workflow", self.workflow_name, "trigger_next_run_at",
		                    frappe.utils.add_to_date(None, minutes=-1), update_modified=False)
		frappe.db.commit()
		# Armed by PATCH, never by writing the switch: a test that flips a real toggle and dies leaves the
		# bench armed, which is exactly how "dormant by default" stops being falsifiable.
		with patch("frappe.enqueue") as enqueue, patch.object(drain, "_armed", return_value=True):
			cohort_sweep_started = drain.sweep()
		self.assertEqual(cohort_sweep_started, 1, "the sweep starts one drain for the one due workflow")
		self.assertEqual(enqueue.call_count, 1, "a cohort must cost ONE enqueue, whatever its size")

	def test_the_drain_starts_one_ordinary_run_per_lead(self):
		drain.run_cohort(self.workflow_name, respect_switch=False)
		self.assertCountEqual(self._runs(), [lead.name for lead in self.leads])

	def test_it_walks_a_keyset_cursor_and_never_offset(self):
		"""OFFSET re-reads every skipped row and shifts rows between pages, so a lead is counted twice or
		missed while the list is written to underneath. The cursor is the lead name, ascending."""
		with patch("frappe.get_all", wraps=frappe.get_all) as get_all:
			drain.run_cohort(self.workflow_name, chunk=3, respect_switch=False)
		# THE SELECTOR's own reads, identified by its projection — it asks for `name` and nothing else.
		# Deliberately not "every CRM Lead read during the drain": instantiating a lead Document makes
		# frappe evaluate `custom_patient_other_programs`, a VIRTUAL FIELD whose `options` expression reads
		# CRM Lead by phone (`base_document._evaluate_virtual_field_options`). That is a field hydration,
		# not pagination, and a lock that cannot tell the two apart reports the lead layer as a drain bug.
		lead_reads = [
			c for c in get_all.call_args_list
			if c.args and c.args[0] == "CRM Lead" and c.kwargs.get("fields") == ["name"]
		]
		self.assertTrue(lead_reads, "the selector's own paged reads were not seen — this would pass vacuously")
		for call in lead_reads:
			self.assertNotIn("start", call.kwargs, "a drain must never paginate by OFFSET")
			self.assertEqual(call.kwargs.get("order_by"), "name asc")


class TestACohortIsNotTruncatedByLeadsItDoesNotWant(_CohortCase):
	"""THE REGRESSION LOCK. `limit` is how many MATCHES the caller wants; it is NOT how far to read.

	An earlier cut applied the chunk size to the grain query and filtered by criteria afterwards, so a
	chunk that happened to open on leads the criteria reject came back empty and the drain concluded the
	cohort was finished. On a real cohort — thousands on a grain, a criteria selecting some of them — that
	starts NOBODY and reports done. The decoys below sort ahead of every wanted lead, so a chunk smaller
	than the decoy count reads nothing but decoys on its first pass.
	"""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		# Same grain, deliberately NOT matching the criteria, and named to sort first.
		cls.decoys = [fx.make_lead(first_name="decoy", name=f"0000-decoy-{i}") for i in range(5)]
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		for decoy in cls.decoys:
			frappe.delete_doc("CRM Lead", decoy.name, force=True, ignore_permissions=True)
		frappe.db.commit()
		super().tearDownClass()

	def test_a_chunk_of_unwanted_leads_does_not_end_the_cohort(self):
		drain.run_cohort(self.workflow_name, chunk=2, respect_switch=False)
		self.assertCountEqual(self._runs(), [lead.name for lead in self.leads],
		                      "every lead the criteria select must be started, whatever sorts ahead of them")

	def test_no_decoy_is_ever_started(self):
		drain.run_cohort(self.workflow_name, chunk=2, respect_switch=False)
		for decoy in self.decoys:
			self.assertNotIn(decoy.name, self._runs())


class TestOnlyOneSweepCanTakeACohort(_CohortCase):
	"""THE GUARANTEE, not the mechanism: two sweeps racing for one due cohort produce exactly one drain.

	Asserted on the OUTCOME — one winner, one enqueue — so it survives however the claim is implemented.
	The claim is a `for_update` row lock whose filter names the state it requires, the shape
	`wakeups.drive_journey` and `signals.resume_for_signal` already use: the loser blocks on the lock,
	re-reads, sees `Draining` and takes nothing. An earlier cut asserted `ROW_COUNT()` instead, which
	tested how the claim was written rather than what it promises.
	"""

	def _make_due(self):
		frappe.db.set_value("CRM Workflow", self.workflow_name, {
			"trigger_next_run_at": frappe.utils.add_to_date(None, minutes=-1), "cohort_state": "",
		}, update_modified=False)
		frappe.db.commit()

	def test_two_claims_produce_exactly_one_winner(self):
		self._make_due()
		verdicts = [drain._claim(self.workflow_name), drain._claim(self.workflow_name)]
		self.assertEqual(verdicts, [True, False], "a cohort already being drained must not be claimed again")
		self.assertEqual(frappe.db.get_value("CRM Workflow", self.workflow_name, "cohort_state"), drain.DRAINING)

	def test_two_sweeps_start_the_cohort_once(self):
		"""The claim's whole purpose, at the level that matters: a cohort is never drained twice."""
		self._make_due()
		with patch("frappe.enqueue") as enqueue, patch.object(drain, "_armed", return_value=True):
			first = drain.sweep()
			second = drain.sweep()
		self.assertEqual((first, second), (1, 0))
		self.assertEqual(enqueue.call_count, 1, "two sweeps must not enqueue two drains for one cohort")

	def test_the_claim_moves_the_clock_so_the_next_tick_skips_it(self):
		"""A workflow still showing due after being claimed is picked up again by the very next tick."""
		self._make_due()
		self.assertTrue(drain._claim(self.workflow_name))
		next_run = frappe.db.get_value("CRM Workflow", self.workflow_name, "trigger_next_run_at")
		self.assertGreater(next_run, frappe.utils.now_datetime())

	def test_a_released_cohort_can_be_claimed_again(self):
		"""The claim is a lease, not a tombstone — next month's cohort must be able to take it."""
		self._make_due()
		self.assertTrue(drain._claim(self.workflow_name))
		drain._release(self.workflow_name)
		self._make_due()
		self.assertTrue(drain._claim(self.workflow_name))


class TestAResumeCannotDoubleStartALead(_CohortCase):
	"""A worker killed mid-drain resumes from the stored cursor. The cursor is the first line; the
	`active_key` UNIQUE index behind `_start_one` is the second, so even a lost cursor cannot double-run."""

	def test_the_cursor_is_stored_as_it_goes(self):
		drain.run_cohort(self.workflow_name, chunk=3, stop_after_chunks=1, respect_switch=False)
		cursor = frappe.db.get_value("CRM Workflow", self.workflow_name, "cohort_cursor")
		self.assertTrue(cursor, "a drain that stores no cursor restarts the cohort from the beginning")
		self.assertEqual(len(self._runs()), 3)

    # A resume must pick up AFTER the cursor, not re-walk from the top.
	def test_resuming_finishes_the_cohort_exactly_once(self):
		drain.run_cohort(self.workflow_name, chunk=3, stop_after_chunks=1, respect_switch=False)
		started_first = set(self._runs())
		drain.run_cohort(self.workflow_name, chunk=3, respect_switch=False)
		all_started = self._runs()
		self.assertEqual(len(all_started), len(set(all_started)), "a lead was started twice")
		self.assertCountEqual(all_started, [lead.name for lead in self.leads])
		self.assertTrue(started_first.issubset(set(all_started)))

	def test_a_second_drain_of_a_finished_cohort_starts_nothing_new(self):
		drain.run_cohort(self.workflow_name, respect_switch=False)
		before = sorted(self._runs())
		drain.run_cohort(self.workflow_name, respect_switch=False)
		self.assertEqual(sorted(self._runs()), before)


class TestTheAbortStopsTheCohortBetweenChunks(_CohortCase):
	"""COHORT-LEVEL, not per-lead: stop starting new runs now, leave the ones already started alone. A
	per-lead cancel is a different need (W10) and is deliberately not built here."""

	def test_an_abort_stops_further_starts(self):
		drain.run_cohort(self.workflow_name, chunk=2, stop_after_chunks=1, respect_switch=False)
		started = len(self._runs())
		self.assertEqual(started, 2)
		drain.abort(self.workflow_name)
		drain.run_cohort(self.workflow_name, chunk=2, respect_switch=False)
		self.assertEqual(len(self._runs()), started, "the abort must stop the drain at the chunk boundary")

	def test_the_abort_leaves_already_started_runs_alone(self):
		drain.run_cohort(self.workflow_name, chunk=2, stop_after_chunks=1, respect_switch=False)
		alive = frappe.get_all(fx.JOURNEY_DT, filters={"workflow": self.workflow_name}, fields=["name", "status"])
		drain.abort(self.workflow_name)
		after = frappe.get_all(fx.JOURNEY_DT, filters={"workflow": self.workflow_name}, fields=["name", "status"])
		self.assertEqual(
			sorted((r.name, r.status) for r in alive), sorted((r.name, r.status) for r in after),
			"a cohort abort must not touch a journey that already started — that is W10's per-lead kill",
		)


class TestNothingIsBornWithTheSwitchOff(_CohortCase):
	"""The switch ARMS this. It ships OFF and a catalog row is a declaration, not an arming."""

	def test_the_cohort_switch_ships_off(self):
		from tatva_connect.automation import settings

		self.assertFalse(settings.is_enabled(drain.SWITCH_COHORT))

	def test_the_sweep_does_nothing_while_the_switch_is_off(self):
		with patch("frappe.enqueue") as enqueue:
			started = drain.sweep(respect_switch=True)
		self.assertEqual(started, 0)
		enqueue.assert_not_called()

	def test_the_drain_itself_refuses_while_the_switch_is_off(self):
		"""The job re-reads the switch, because it may have been turned off between the sweep and the job
		running — the same re-check `triggers.start_journey` makes, and for the same reason."""
		drain.run_cohort(self.workflow_name, respect_switch=True)
		self.assertEqual(self._runs(), [])


class TestTheKillSwitchReachesARunningDrain(_CohortCase):
	"""THE SAFETY PROPERTY THIS CHUNK WAS BUILT AROUND, and it did not hold.

	An operator disabling the engine expects the cohort to stop. Two separate defects meant it did not,
	and both are asserted below because either one alone is enough to keep a drain running:

	  * `run_cohort`'s `respect_switch` defaulted to FALSE and the sweep's own enqueue never passed it, so
	    the queued job — the ONLY caller in production — read no switch at all. The docstring claimed the
	    opposite.
	  * even asked to respect it, `_armed()` was read ONCE before the loop. The loop re-read `cohort_abort`
	    and nothing else, while the commit message said "the switch and the abort flag re-read at every
	    boundary".

	The abort flag is not a substitute. It is per-workflow and nobody reaches for it after hitting a
	global kill switch — which is the whole point of having one.
	"""

	def test_the_job_the_sweep_queues_refuses_once_the_switch_goes_off(self):
		"""Driven through the REAL kwargs the sweep enqueues, not a hand-written call: the defect was
		precisely that the sweep omitted an argument, so a test that supplied it could never see this."""
		# The fixture's schedule is Daily 09:00, so it is only due for part of the day. Drive the clock
		# the way every other sweep test here does, or `_due_workflows` finds nothing and the assertion
		# below never reaches the code it is about.
		frappe.db.set_value("CRM Workflow", self.workflow_name, "trigger_next_run_at",
		                    frappe.utils.add_to_date(None, minutes=-1), update_modified=False)
		frappe.db.commit()
		with patch("frappe.enqueue") as enqueue, patch.object(drain, "_armed", return_value=True):
			drain.sweep()
		job = dict(enqueue.call_args.kwargs)
		for plumbing in ("queue", "job_id", "deduplicate", "now"):
			job.pop(plumbing, None)
		# The switch is OFF from here — exactly the window between sweep and job the docstring names.
		drain.run_cohort(**job)
		self.assertEqual(self._runs(), [],
		                 "the queued drain walked the whole cohort after the switch was turned off")

	def test_the_switch_stops_a_drain_that_is_already_walking(self):
		"""Armed for the first chunk, off from then on. A drain that reads the switch once cannot see this
		and runs to the end of the cohort."""
		reads = {"n": 0}

		def armed():
			reads["n"] += 1
			return reads["n"] <= 1

		with patch.object(drain, "_armed", side_effect=armed):
			started = drain.run_cohort(self.workflow_name, chunk=2, respect_switch=True)
		self.assertGreater(reads["n"], 1, "the switch was read once and never again — no boundary re-reads it")
		self.assertLess(started, _LEADS, "the kill switch did not reach a drain that was already walking")
		self.assertEqual(len(self._runs()), started)

	def test_a_drain_the_switch_stopped_hands_its_cohort_back(self):
		"""Stopping must not strand the claim. A cohort left `Draining` is one `_claim` can never match
		again, so the kill switch would trade a running drain for a permanently dead one.

		NOT a red-first proof and it is not claimed as one: today's code never breaks mid-loop, so it
		reaches the end and releases anyway. This guards the FIX — the obvious way to write the re-read is
		a bare `break`, which leaves the claim held. Related: the same stranding by another route is
		`docs/pending/2026-07-28-cohort-drain-leaks-its-claim-on-failure.md`.
		"""
		with patch.object(drain, "_armed", side_effect=[True, False]):
			drain.run_cohort(self.workflow_name, chunk=2, respect_switch=True)
		self.assertEqual(
			frappe.db.get_value("CRM Workflow", self.workflow_name, "cohort_state"), drain.IDLE,
			"a stopped drain kept the claim, so this cohort can never be drained again",
		)


class TestPacingRidesTheExistingBucket(_CohortCase):
	"""The provider is the binding constraint — the live trial hit WATI's rate limit with a handful. The
	drain charges the SAME atomic Redis bucket the partner API uses; there is no second limiter."""

	def test_a_refused_token_stops_the_chunk_rather_than_dropping_leads(self):
		with patch("tatva_connect.workflow_engine.drain._take_token", return_value=False):
			drain.run_cohort(self.workflow_name, chunk=3, respect_switch=False)
		self.assertEqual(self._runs(), [], "a refused token must PAUSE the cohort, never skip a lead")
		self.assertFalse(frappe.db.get_value("CRM Workflow", self.workflow_name, "cohort_cursor"),
		                 "a lead that was never started must not be behind the cursor")

	def test_the_bucket_is_the_partner_apis_own(self):
		import inspect

		source = inspect.getsource(drain)
		self.assertIn("_bucket_pair", source, "pacing must charge the existing bucket, not a second one")
