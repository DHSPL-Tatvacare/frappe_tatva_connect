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

from tatva_connect.workflow_engine import drain, registry, thresholds
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

	def tearDown(self):
		self._reset()

	def _reset(self):
		for run in frappe.get_all(fx.JOURNEY_DT, filters={"workflow": self.workflow_name}, pluck="name"):
			frappe.db.delete(fx.STEP_LOG_DT, {"journey": run})
		frappe.db.delete(fx.JOURNEY_DT, {"workflow": self.workflow_name})
		frappe.db.set_value("CRM Workflow", self.workflow_name, {
			"cohort_cursor": "", "cohort_abort": 0, "cohort_state": "",
			"cohort_next_chunk_at": None, "cohort_interval_seconds": 0,
		}, update_modified=False)
		frappe.db.commit()

	def _runs(self):
		return frappe.get_all(fx.JOURNEY_DT, filters={"workflow": self.workflow_name}, pluck="subject_name")

	def _drain_all(self, chunk=None, limit=20, book=False):
		"""Walk the cohort to the end, one chunk per call — what its own bookings do, without the waiting.

		ONE JOB IS ONE CHUNK now, so a test that wants a finished cohort has to drive the chunks; a single
		`run_cohort` asserting the whole cohort started would be asserting the old loop. Booking is OFF by
		default so a test about the WALK does not depend on Redis holding a job between calls; a test about
		the booking itself passes `book=True` and gets the production path.
		"""
		for _ in range(limit):
			drain.run_cohort(self.workflow_name, chunk=chunk, respect_switch=False, book_next=book)
			if not frappe.db.get_value("CRM Workflow", self.workflow_name, "cohort_cursor"):
				return
		self.fail(f"the cohort did not finish within {limit} chunks")


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
		self._drain_all()
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
		self._drain_all(chunk=2)
		self.assertCountEqual(self._runs(), [lead.name for lead in self.leads],
		                      "every lead the criteria select must be started, whatever sorts ahead of them")

	def test_no_decoy_is_ever_started(self):
		self._drain_all(chunk=2)
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

	def test_the_claim_leaves_the_clock_alone_and_the_state_is_what_skips_it(self):
		"""REPLACES a test that asserted the claim moved the clock forward. It did, and that write is what
		broke the scheduled lane (A1): a walk that paused for pacing was already dated tomorrow, so the
		sweep never came back for it. The promise the old test named — the next tick does not drain this
		cohort twice — is kept by the STATE, under the row lock, and `test_two_sweeps_start_the_cohort_once`
		is the assertion of it at the level that matters."""
		self._make_due()
		self.assertTrue(drain._claim(self.workflow_name))
		next_run = frappe.db.get_value("CRM Workflow", self.workflow_name, "trigger_next_run_at")
		self.assertLessEqual(next_run, frappe.utils.now_datetime(),
		                     "the claim moved the clock, so a paused walk falls out of the due set")

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
		drain.run_cohort(self.workflow_name, chunk=3, respect_switch=False, book_next=False)
		cursor = frappe.db.get_value("CRM Workflow", self.workflow_name, "cohort_cursor")
		self.assertTrue(cursor, "a drain that stores no cursor restarts the cohort from the beginning")
		self.assertEqual(len(self._runs()), 3)

    # A resume must pick up AFTER the cursor, not re-walk from the top.
	def test_resuming_finishes_the_cohort_exactly_once(self):
		drain.run_cohort(self.workflow_name, chunk=3, respect_switch=False, book_next=False)
		started_first = set(self._runs())
		self._drain_all(chunk=3)
		all_started = self._runs()
		self.assertEqual(len(all_started), len(set(all_started)), "a lead was started twice")
		self.assertCountEqual(all_started, [lead.name for lead in self.leads])
		self.assertTrue(started_first.issubset(set(all_started)))

	def test_a_second_drain_of_a_finished_cohort_starts_nothing_new(self):
		self._drain_all()
		before = sorted(self._runs())
		self._drain_all()
		self.assertEqual(sorted(self._runs()), before)


class TestTheAbortStopsTheCohortBetweenChunks(_CohortCase):
	"""COHORT-LEVEL, not per-lead: stop starting new runs now, leave the ones already started alone. A
	per-lead cancel is a different need (W10) and is deliberately not built here."""

	def test_an_abort_stops_further_starts(self):
		drain.run_cohort(self.workflow_name, chunk=2, respect_switch=False, book_next=False)
		started = len(self._runs())
		self.assertEqual(started, 2)
		drain.abort(self.workflow_name)
		drain.run_cohort(self.workflow_name, chunk=2, respect_switch=False)
		self.assertEqual(len(self._runs()), started, "the abort must stop the drain at the chunk boundary")

	def test_the_abort_leaves_already_started_runs_alone(self):
		drain.run_cohort(self.workflow_name, chunk=2, respect_switch=False, book_next=False)
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
		for plumbing in ("queue", "job_id", "deduplicate", "enqueue_after_commit", "now"):
			job.pop(plumbing, None)
		# The switch is OFF from here — exactly the window between sweep and job the docstring names.
		drain.run_cohort(**job)
		self.assertEqual(self._runs(), [],
		                 "the queued drain walked the whole cohort after the switch was turned off")

	def test_the_switch_stops_a_cohort_between_chunks(self):
		"""Armed for the first chunk, off for the second. The boundary the switch has to reach is now the
		gap BETWEEN jobs rather than between loop passes, and it is read at the top of every one of them."""
		with patch.object(drain, "_armed", side_effect=[True, False]):
			first = drain.run_cohort(self.workflow_name, chunk=2, respect_switch=True, book_next=False)
			second = drain.run_cohort(self.workflow_name, chunk=2, respect_switch=True, book_next=False)

		self.assertEqual(first, 2, "the armed chunk must start its leads")
		self.assertEqual(second, 0, "the kill switch did not reach the next chunk of a running cohort")
		self.assertEqual(len(self._runs()), 2)

	def test_a_cohort_the_switch_stopped_books_nothing_and_hands_itself_back(self):
		"""Stopping must not strand the claim, AND must not leave a booking behind to resume it.

		A cohort left `Draining` is one `_claim` can never match again, so the kill switch would trade a
		running drain for a permanently dead one. The booking is the second half: an alarm surviving the
		switch would walk the cohort on a minute later, which is the switch not working.
		"""
		with patch.object(drain, "_armed", return_value=False):
			drain.run_cohort(self.workflow_name, chunk=2, respect_switch=True)

		row = frappe.db.get_value("CRM Workflow", self.workflow_name,
		                          ["cohort_state", "cohort_next_chunk_at"], as_dict=True)
		self.assertEqual(row.cohort_state, drain.IDLE,
		                 "a stopped drain kept the claim, so this cohort can never be drained again")
		self.assertIsNone(row.cohort_next_chunk_at, "a switched-off cohort still owes a next chunk")


class TestTheDrainBooksItsOwnNextChunk(_CohortCase):
	"""THE PACE IS THE DRAIN'S OWN, and this is the half that makes that true.

	It used to walk until a Redis token bucket ran dry and then stop dead, leaving the */15 sweep to bring
	it back: one chunk per SWEEP, not per minute, so a cohort delivered a fifteenth of the rate its own
	constant declared and nothing in the code said so. The booking replaces the bucket entirely — the pace
	is now `DRAIN_CHUNK` leads every `DRAIN_INTERVAL_SECONDS`, two numbers that mean what they say.

	`cohort_next_chunk_at` is asserted rather than the RQ registry: the row is the durable half of the
	booking (§6.2 — the job in Redis is a copy, never the fact), and it is what `_due_workflows` reads.
	"""

	def _pending(self):
		return frappe.db.get_value("CRM Workflow", self.workflow_name, "cohort_next_chunk_at")

	def test_a_chunk_with_work_left_books_the_next_one(self):
		drain.run_cohort(self.workflow_name, chunk=2, respect_switch=False)

		self.assertIsNotNone(self._pending(), "a paused cohort booked nothing, so only the sweep can resume it")
		self.assertGreater(self._pending(), frappe.utils.now_datetime(),
		                   "the next chunk is owed in the past — the pace is not being waited out")

	def test_the_interval_is_the_one_the_operator_set(self):
		"""The pace comes from the settings row, not from a number inside the drain."""
		frappe.db.set_value("CRM Workflow", self.workflow_name, "cohort_interval_seconds", 300,
		                    update_modified=False)
		frappe.db.commit()

		drain.run_cohort(self.workflow_name, chunk=2, respect_switch=False)

		booked = (self._pending() - frappe.utils.now_datetime()).total_seconds()
		self.assertGreater(booked, 240, "a workflow's own interval was ignored in favour of the default")

	def test_a_finished_cohort_books_no_further_chunk(self):
		"""The occurrence is over, so the next thing owed is the next OCCURRENCE — not another chunk.

		Driven with booking ON, or this asserts nothing: with it off a finished cohort would read clear here
		however the finishing path behaved.
		"""
		self._drain_all(book=True)

		self.assertIsNone(self._pending(), "a finished cohort still owes a chunk and will walk again in a minute")
		self.assertEqual(frappe.db.get_value("CRM Workflow", self.workflow_name, "cohort_state"), drain.IDLE,
		                 "a finished cohort kept its claim, so it can never be drained again")

	def test_the_claim_is_held_from_the_first_chunk_to_the_last(self):
		"""THE OPERATOR'S STOP BUTTON, which renders on `cohort_state === 'Draining'` and nothing else.

		Releasing between chunks left the flag clear for all but the first seconds of a multi-hour cohort, so
		the only way to stop a running cohort vanished from the screen. Claimed first, exactly as the sweep
		does it, because the claim is the sweep's to take and the chunks' only job is not to drop it.
		"""
		self.assertTrue(drain._claim(self.workflow_name))
		drain.run_cohort(self.workflow_name, chunk=2, respect_switch=False)

		self.assertEqual(
			frappe.db.get_value("CRM Workflow", self.workflow_name, "cohort_state"), drain.DRAINING,
			"a cohort with leads still to start reads as idle — the Stop button is gone and the sweep can take it",
		)

	def test_a_walking_cohort_cannot_be_claimed_a_second_time(self):
		"""The same flag, at the level that matters: two walkers on one cursor is the thing it prevents."""
		self.assertTrue(drain._claim(self.workflow_name))
		drain.run_cohort(self.workflow_name, chunk=2, respect_switch=False)

		self.assertFalse(drain._claim(self.workflow_name),
		                 "a cohort that was already walking was handed to a second walker")


class TestACohortComesBackUntilItIsFinished(_CohortCase):
	"""A1 — THE SCHEDULED LANE, which did not work above one chunk.

	`_claim` advanced `trigger_next_run_at` to the next occurrence at claim time. A walk that paused then
	released a row already dated tomorrow, so `_due_workflows` could not see it and the sweep never came
	back: a cohort larger than one chunk started ~60 journeys PER OCCURRENCE and a Daily 10,000 took months.

	It failed silently, which is why this suite never caught it. The cursor, the counts, `cohort_state` and
	the receipt were all correct at every step. The only wrong fact was a datetime that read like a schedule.
	"""

	def _make_due(self):
		frappe.db.set_value("CRM Workflow", self.workflow_name, {
			"trigger_next_run_at": frappe.utils.add_to_date(None, minutes=-1), "cohort_state": "",
		}, update_modified=False)
		frappe.db.commit()

	def test_a_walk_that_paused_is_still_due(self):
		"""The defect in one assertion: a pause is not a completed occurrence, so the clock must not move."""
		self._make_due()
		drain.run_cohort(self.workflow_name, chunk=3, respect_switch=False, book_next=False)

		row = frappe.db.get_value("CRM Workflow", self.workflow_name,
		                          ["cohort_state", "trigger_next_run_at"], as_dict=True)
		self.assertEqual(row.cohort_state, drain.IDLE, "a paused walk must hand its claim back")
		self.assertLessEqual(row.trigger_next_run_at, frappe.utils.now_datetime(),
		                     "a paused cohort was rescheduled, so no later sweep will pick it up")

	def test_a_finished_walk_does_move_the_clock(self):
		"""The other half, or the fix would just make every cohort run for ever: a walk that reached the end
		of its cohort HAS completed its occurrence, and waits for the next one."""
		self._make_due()
		self._drain_all()

		self.assertGreater(
			frappe.db.get_value("CRM Workflow", self.workflow_name, "trigger_next_run_at"),
			frappe.utils.now_datetime(),
			"a completed cohort stayed due and will be walked again on the very next tick",
		)

	def test_the_sweep_starts_the_cohort_and_the_chunks_finish_it(self):
		"""THE DELIVERABLE, and the division of labour it now rests on: the sweep opens the cohort, the
		BOOKINGS carry it to the end. Every lead started, nobody twice.

		The sweep's own `now=` makes its enqueue run the drain inline (`background_jobs.py:152` short-circuits
		before `enqueue_after_commit`), so the first chunk really is the sweep's own path and not a
		hand-written call — the distinction that let the original defect hide. From there the sweep must NOT
		be the thing that resumes it: the claim is held, so `_claim` correctly declines and the booking is
		what delivers each later chunk. `_drain_all` stands in for RQ's scheduler firing them.
		"""
		self._make_due()
		with patch.object(drain, "_armed", return_value=True), patch.object(drain, "_pause"):
			drain.sweep()
		first_chunk = len(self._runs())

		self._drain_all(chunk=2)

		started = self._runs()
		self.assertGreater(first_chunk, 0, "the sweep started no chunk at all — the rest proves nothing")
		self.assertCountEqual(
			started, [lead.name for lead in self.leads],
			"the cohort did not finish across chunks — a paused walk was never picked up again",
		)
		self.assertEqual(len(started), len(set(started)), "a lead was started twice across chunks")


class TestAClaimWhoseWalkerDiedIsHandedBack(_CohortCase):
	"""A2 — `_release` had exactly ONE caller, the `run_cohort` job itself. An OOM, a deploy restart or an
	RQ timeout left `cohort_state` on `Draining`, every later `_claim` lost, and that workflow's cohort
	never ran again until somebody wrote to the database by hand. W4.2 promised this reaper and it was
	never built; `docs/pending/2026-07-28-cohort-drain-leaks-its-claim-on-failure.md` raised it again.

	The heartbeat it reads is not new machinery: `run_cohort` already writes the cursor as it walks, and
	`cohort_progress_at` now rides that same write.
	"""

	def _strand(self, minutes_ago):
		values = {"cohort_state": drain.DRAINING, "cohort_progress_at": None}
		if minutes_ago is not None:
			values["cohort_progress_at"] = frappe.utils.add_to_date(
				frappe.utils.now_datetime(), minutes=-minutes_ago,
			)
		frappe.db.set_value("CRM Workflow", self.workflow_name, values, update_modified=False)
		frappe.db.commit()

	def _sweep(self):
		with patch.object(drain, "_armed", return_value=True), patch("frappe.enqueue"):
			drain.sweep()

	def _state(self):
		return frappe.db.get_value("CRM Workflow", self.workflow_name, "cohort_state")

	def test_a_dead_claim_is_reaped(self):
		self._strand(thresholds.DRAIN_DEAD_AFTER_MINUTES + 1)

		self._sweep()

		self.assertEqual(self._state(), drain.IDLE,
		                 "a claim whose walker died is held for ever and no later claim can ever match it")

	def test_a_cohort_waiting_out_a_long_interval_is_not_reaped(self):
		"""A cohort between chunks is deliberately still, and the operator may set that stillness as high as
		an hour — twice the dead-age. Judged on silence it would be torn down mid-walk on every sweep, which
		is the defect holding the claim would otherwise have introduced. It is judged on the BOOKING."""
		frappe.db.set_value("CRM Workflow", self.workflow_name, {
			"cohort_state": drain.DRAINING,
			"cohort_progress_at": frappe.utils.add_to_date(None, minutes=-(thresholds.DRAIN_DEAD_AFTER_MINUTES + 5)),
			"cohort_next_chunk_at": frappe.utils.add_to_date(None, minutes=10),
		}, update_modified=False)
		frappe.db.commit()

		self._sweep()

		self.assertEqual(self._state(), drain.DRAINING,
		                 "a healthy cohort waiting for its next batch was reaped as a dead worker")

	def test_a_booking_that_never_arrived_is_reaped(self):
		"""The other direction: the batch was owed long ago and nothing delivered it, so the walker is gone."""
		frappe.db.set_value("CRM Workflow", self.workflow_name, {
			"cohort_state": drain.DRAINING,
			"cohort_next_chunk_at": frappe.utils.add_to_date(None, minutes=-(thresholds.DRAIN_DEAD_AFTER_MINUTES + 5)),
		}, update_modified=False)
		frappe.db.commit()

		self._sweep()

		self.assertEqual(self._state(), drain.IDLE, "a lost booking left the cohort claimed for ever")

	def test_an_unfinished_cohort_resumes_today_rather_than_tomorrow(self):
		"""What the reaper must NOT do now that a job is one chunk: date an interrupted cohort tomorrow.

		The cursor says leads remain, so rescheduling silently drops every one the walk had not reached. It
		releases WITHOUT the clock instead, leaving the row due for the very next sweep to resume.
		"""
		frappe.db.set_value("CRM Workflow", self.workflow_name, {
			"cohort_state": drain.DRAINING, "cohort_cursor": self.leads[0].name,
			"trigger_next_run_at": frappe.utils.add_to_date(None, minutes=-1),
			"cohort_next_chunk_at": frappe.utils.add_to_date(None, minutes=-(thresholds.DRAIN_DEAD_AFTER_MINUTES + 5)),
		}, update_modified=False)
		frappe.db.commit()

		self._sweep()

		row = frappe.db.get_value("CRM Workflow", self.workflow_name,
		                          ["cohort_state", "cohort_cursor", "trigger_next_run_at"], as_dict=True)
		self.assertEqual(row.cohort_state, drain.IDLE)
		self.assertEqual(row.cohort_cursor, self.leads[0].name, "the resume point was thrown away")
		self.assertLessEqual(row.trigger_next_run_at, frappe.utils.now_datetime(),
		                     "an unfinished cohort was dated tomorrow, so the leads it never reached are lost")

	def test_a_finished_cohort_that_died_still_moves_the_clock(self):
		"""The other half, or every reaped cohort would be walked again for ever: no cursor means the walk
		had nothing left to do, so its occurrence really is over."""
		frappe.db.set_value("CRM Workflow", self.workflow_name, {
			"cohort_state": drain.DRAINING, "cohort_cursor": "",
			"cohort_next_chunk_at": frappe.utils.add_to_date(None, minutes=-(thresholds.DRAIN_DEAD_AFTER_MINUTES + 5)),
		}, update_modified=False)
		frappe.db.commit()

		self._sweep()

		self.assertGreater(
			frappe.db.get_value("CRM Workflow", self.workflow_name, "trigger_next_run_at"),
			frappe.utils.now_datetime(),
			"a cohort with nothing left stayed due and will be re-walked on the next tick",
		)

	def test_a_claim_from_before_the_heartbeat_existed_is_reaped(self):
		"""A row already `Draining` when this column shipped has no stamp at all. It is stranded by
		definition — nothing will ever write its heartbeat, because its walker is long gone."""
		self._strand(None)

		self._sweep()

		self.assertEqual(self._state(), drain.IDLE)

	def test_a_walk_that_is_still_moving_is_left_alone(self):
		"""The reaper must not free a claim from under a live worker. Progress inside the age is a walker
		that is working, not one that died."""
		self._strand(1)

		self._sweep()

		self.assertEqual(self._state(), drain.DRAINING,
		                 "the reaper took a cohort away from a worker that was still walking it")

	def _beating(self):
		return frappe.db.get_value("CRM Workflow", self.workflow_name, "cohort_progress_at")

	def test_the_walk_stamps_its_own_heartbeat(self):
		"""Without this the reaper reads a column nothing writes and kills every live drain at the age."""
		self._strand(thresholds.DRAIN_DEAD_AFTER_MINUTES + 1)

		drain.run_cohort(self.workflow_name, chunk=2, respect_switch=False, book_next=False)

		self.assertGreater(
			self._beating(), frappe.utils.add_to_date(frappe.utils.now_datetime(), minutes=-1),
			"the walk moved the cursor without moving its heartbeat, so the reaper will free it mid-walk",
		)

	def test_the_heartbeat_beats_per_lead_not_only_at_the_chunk_boundary(self):
		"""The chunk-boundary stamp alone passes the test above, which is why this one exists separately.

		Inside a chunk the per-lead write is the ONLY thing moving the heartbeat, so a slow chunk would age
		past the reaper and be freed from under a live worker. Read MID-WALK, from inside `_start_one`: the
		boundary write has not happened yet at that point, so a stamp seen there can only have come from the
		previous lead's own write."""
		self._strand(thresholds.DRAIN_DEAD_AFTER_MINUTES + 1)
		stale = self._beating()
		mid_walk = []
		start_one = drain._start_one

		def watched(*args):
			mid_walk.append(self._beating())
			return start_one(*args)

		with patch.object(drain, "_start_one", side_effect=watched):
			drain.run_cohort(self.workflow_name, chunk=5, respect_switch=False, book_next=False)

		self.assertGreater(len(mid_walk), 1, "fewer than two leads were walked — this would pass vacuously")
		self.assertGreater(
			mid_walk[1], stale,
			"the heartbeat had not moved by the second lead, so only the chunk boundary beats",
		)
