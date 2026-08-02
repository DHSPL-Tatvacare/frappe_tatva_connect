# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""W10 — SUSPENDING A WORKFLOW *IS* KILLING IT. One action, one confirm, one outcome.

The dangerous state this deletes is a "Suspended" workflow whose journeys keep messaging patients: the
operator believes it is stopped, and it is not. So Suspended means NOTHING IS IN FLIGHT, always, with no
window in between — and this suite is shaped around that sentence rather than around the code under it.

THE RULE IS ABOUT AVAILABILITY, NOT ABOUT ONE VERB. A workflow that stops being available kills its
journeys; a workflow being EDITED does not. Suspend, Archive and a forced delete are three ways to become
unavailable and they share one kill, which is why they are one suite: `TestRetiringAWorkflowKillsItTheSameWay`
and `TestRevisingLeavesJourneysRunning` are the two halves of that sentence, and a second kill path is the
defect they exist to catch.

TWO GUARANTEES, AND THEY ARE NOT THE SAME ONE. The rows are stopped (a drain, chunked, behind the
lifecycle), and no journey of a Suspended workflow is CLAIMABLE (one condition at the one wake door, true
the instant the lifecycle commits). The second is what makes the first bookkeeping rather than a race —
so it is tested WITHOUT the first, by suspending the header directly and leaving the rows parked.

THE ZOMBIE TEST IS THE ONE THAT MATTERS. Asserting a status column after calling the killer proves the
killer wrote a column. What has to be true is that the two things that WAKE a journey — the real
`wakeups.sweep()` and a real signal through `signals.resume_for_signal` — find nothing. Driving the
interpreter directly proves nothing, because that is not how a parked journey resumes.

STOPPED IS TERMINAL, NOT A DELETION — the same terminal transition `stop_for_subject` already made for a
deleted lead, keyed on the workflow instead of the subject. The row stays readable and says why; what
goes is its ability to act. A test that checked `status` alone would pass on a stop that left the journey
wakeable, which is why every assertion here reads all four columns.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \\
        --module tatva_connect.workflow_engine.tests.test_suspend_is_kill
"""
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.tatva_connect.doctype.crm_workflow.crm_workflow import ACTIVE, ARCHIVED, SUSPENDED
from tatva_connect.workflow_engine import SWEEP_SWITCH, drain, interpreter, signals, thresholds, wakeups
from tatva_connect.workflow_engine.tests import fixtures
from tatva_connect.workflows import api as workflows_api

JOURNEY_DT = fixtures.JOURNEY_DT
WORKFLOW_DT = fixtures.WORKFLOW_DT
_WF = "ZZ Suspend Is Kill"
_BYSTANDER = "ZZ Suspend Bystander"
_DOOMED = "ZZ Suspend Doomed"
_RETIRED = "ZZ Suspend Archived"
_REVISED = "ZZ Suspend Revised"
_SIGNAL = "document.uploaded"


def _graph():
	"""The smallest workflow that can hold a journey — a Trigger into a Terminal."""
	return [fixtures.trigger(to="n1"), fixtures.node("n1", "Terminal")]


def _arm_sweep(cls):
	"""The sweep switch, armed for this class and registered OFF again — `arm_engine`'s reasoning, verbatim.

	The restore goes to OFF and never to "whatever it was": restoring the previous value is what propagates
	a poisoned baseline, and every switch in this app is dormant at rest.
	"""
	cls.addClassCleanup(_set_sweep, False)
	_set_sweep(True)


def _set_sweep(enabled):
	frappe.db.set_value("CRM Tatva Automation", SWEEP_SWITCH, "enabled", 1 if enabled else 0)
	frappe.db.commit()


class _KillCase(FrappeTestCase):
	"""Shared scaffolding: the parked-journey fixture, the state reader and the stop assertion."""

	def _park(self, workflow, lead_name):
		"""A journey parked on a timer AND on a signal — every column a stop must clear is populated, and
		both wake paths have something real to find."""
		journey = fixtures.start_journey(workflow, lead_name, "n1")
		frappe.db.set_value(JOURNEY_DT, journey.name, {
			"status": "Parked",
			"resume_at": frappe.utils.add_to_date(frappe.utils.now_datetime(), days=-1),
			"awaiting_signal": _SIGNAL,
			"awaiting_correlation": f"{journey.name}::n1",
			"active_key": f"{workflow.name}::{lead_name}",
		}, update_modified=False)
		frappe.db.commit()
		return journey

	def _lead(self):
		"""A probe lead this test owns and destroys — an orphan lead makes an unrelated suite look broken."""
		lead = fixtures.make_lead()
		self.addCleanup(self._drop_lead, lead.name)
		return lead

	def _drop_lead(self, name):
		if frappe.db.exists("CRM Lead", name):
			frappe.delete_doc("CRM Lead", name, force=True, ignore_permissions=True)
			frappe.db.commit()

	def _state(self, journey_name):
		return frappe.db.get_value(
			JOURNEY_DT, journey_name,
			["status", "stop_reason", "active_key", "resume_at", "awaiting_signal", "awaiting_correlation"],
			as_dict=True,
		)

	def _assert_stopped(self, journey_name, reason_contains):
		state = self._state(journey_name)
		self.assertEqual(state.status, interpreter.STOPPED, f"{journey_name} is {state.status}, not Stopped")
		self.assertIn(reason_contains, state.stop_reason or "", "a stopped journey must say why")
		for column in ("active_key", "resume_at", "awaiting_signal", "awaiting_correlation"):
			self.assertIsNone(state[column], f"{column} survived the stop — the journey is still wakeable")


class TestSuspendIsKill(_KillCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		fixtures.purge(_WF, _BYSTANDER)
		cls.addClassCleanup(fixtures.purge, _WF, _BYSTANDER)
		fixtures.arm_engine(True, cls=cls)
		_arm_sweep(cls)
		cls.workflow = fixtures.make_workflow(_WF, _graph())
		cls.bystander = fixtures.make_workflow(_BYSTANDER, _graph())

	def setUp(self):
		self.addCleanup(self._reset)
		# Two leads, because `active_key` is UNIQUE per (workflow, lead) — one lead cannot hold two live
		# journeys of the same workflow, which is the guarantee the kill then has to free.
		self.lead = self._lead()
		self.journeys = [
			self._park(self.workflow, self.lead.name),
			self._park(self.workflow, self._lead().name),
		]

	def _reset(self):
		"""Registered, never a tearDown: `tearDown` is skipped when setUp raises, and that is how the last
		leak of orphan rows onto the one working bench happened."""
		for name in frappe.get_all(JOURNEY_DT, filters={"workflow": ["in", [_WF, _BYSTANDER]]}, pluck="name"):
			frappe.db.delete(fixtures.STEP_LOG_DT, {"journey": name})
		frappe.db.delete(JOURNEY_DT, {"workflow": ["in", [_WF, _BYSTANDER]]})
		frappe.db.set_value(WORKFLOW_DT, _WF, {
			"lifecycle_state": ACTIVE, "cohort_state": "", "cohort_abort": 0,
		}, update_modified=False)
		frappe.db.commit()

	def _suspend(self):
		return workflows_api.suspend(_WF)

	# ---- the kill ------------------------------------------------------------------------------

	def test_suspending_stops_every_live_journey_of_the_workflow(self):
		"""The whole feature in one assertion: the lifecycle moved AND nothing is in flight behind it."""
		result = self._suspend()

		self.assertEqual(result["lifecycle_state"], SUSPENDED)
		for journey in self.journeys:
			self._assert_stopped(journey.name, "suspended")

	def test_the_receipt_says_how_many_it_stopped(self):
		"""An operator told nothing cannot tell a kill that worked from one that found no rows."""
		self.assertEqual(self._suspend()["stopping"], len(self.journeys))

	def test_suspending_is_not_gated_on_the_engine_switch(self):
		"""Killing is CLEANUP, not engine activity. A dormant engine must not leave journeys alive — which
		is the exact opposite of the lead-delete stop, and deliberately so."""
		fixtures.arm_engine(False)
		try:
			self._suspend()
		finally:
			fixtures.arm_engine(True)
		for journey in self.journeys:
			self._assert_stopped(journey.name, "suspended")

	def test_a_terminal_journey_is_untouched(self):
		"""A delivered message is not un-delivered: Done stays Done and gains no stop reason."""
		done = self.journeys[0]
		frappe.db.set_value(JOURNEY_DT, done.name, {"status": "Done", "active_key": None}, update_modified=False)
		frappe.db.commit()

		self._suspend()

		state = self._state(done.name)
		self.assertEqual(state.status, "Done", "a terminal journey was transitioned a second time")
		self.assertFalse(state.stop_reason, "a Done journey was given a stop reason it never had")
		self._assert_stopped(self.journeys[1].name, "suspended")

	def test_another_workflows_journeys_are_left_alone(self):
		"""The kill is scoped to ONE workflow. A stop that swept by lead would end a stranger's journey."""
		bystander = self._park(self.bystander, self.lead.name)

		self._suspend()

		self.assertEqual(self._state(bystander.name).status, "Parked", "a bystander workflow's journey died")

	def test_stopping_twice_changes_nothing_and_reports_nothing(self):
		"""The lifecycle refuses a second Suspend outright, so idempotency is a property of the KILL."""
		first = interpreter.stop_for_workflow(_WF, "first pass")
		second = interpreter.stop_for_workflow(_WF, "second pass")

		self.assertEqual(first, len(self.journeys), "the first pass must stop every live journey")
		self.assertEqual(second, 0, "a second pass re-stopped an already-terminal journey")
		self.assertIn("first pass", self._state(self.journeys[0].name).stop_reason)

	# ---- THE ZOMBIE TEST -----------------------------------------------------------------------

	def test_nothing_wakes_a_suspended_workflows_journeys(self):
		"""The one that matters. Both real wake paths, driven for real, after the suspend.

		A journey parked on a timer that is already due AND on a signal that is then really delivered. The
		sweep is the timer path and `resume_for_signal` is the signal path; between them they are every way
		a parked journey resumes. If either advances one of these, a patient gets a message from a workflow
		the operator switched off.
		"""
		due_before = set(wakeups._due_parked())
		for journey in self.journeys:
			self.assertIn(journey.name, due_before, "the fixture must be genuinely due, or this proves nothing")

		self._suspend()

		wakeups.sweep()
		correlation = f"{self.journeys[0].name}::n1"
		signals.deliver_signal("CRM Lead", self.lead.name, _SIGNAL, correlation=correlation)
		signals.resume_for_signal("CRM Lead", self.lead.name, _SIGNAL, correlation)

		for journey in self.journeys:
			self._assert_stopped(journey.name, "suspended")
			self.assertEqual(
				fixtures.logs(journey.name), [],
				f"{journey.name} executed a node after the workflow was suspended",
			)

	def test_a_suspended_workflows_journey_is_not_claimable_even_while_still_parked(self):
		"""The second guarantee, tested WITHOUT the first — the rows are deliberately left Parked.

		This is what makes "Suspended means nothing is in flight" true at the lifecycle commit rather than
		whenever a drain happens to finish. Without it there is a window in which the state says Suspended
		and journeys are still sending, which is the exact lie this feature exists to remove.
		"""
		frappe.db.set_value(WORKFLOW_DT, _WF, "lifecycle_state", SUSPENDED, update_modified=False)
		frappe.db.commit()

		wakeups.drive_journey(self.journeys[0].name)

		self.assertEqual(
			self._state(self.journeys[0].name).status, "Parked",
			"a suspended workflow's journey was claimed and driven",
		)

	def test_an_active_workflows_journey_is_still_claimable(self):
		"""The other direction, or the gate above could be satisfied by refusing every journey."""
		wakeups.drive_journey(self.journeys[0].name)

		self.assertNotEqual(
			self._state(self.journeys[0].name).status, "Parked",
			"an ACTIVE workflow's parked journey was not driven — the gate is refusing everything",
		)

	# ---- volume and concurrency ----------------------------------------------------------------

	def test_a_large_kill_costs_exactly_one_enqueue(self):
		"""`frappe.enqueue` THROWS above MAX_QUEUED_JOBS=500, so a kill whose enqueues scale with its
		journeys errors partway and leaves the rest alive — the worst shape available, because it looks
		like it worked. The chunk is forced below the row count so the drain really loops."""
		self.journeys.append(self._park(self.workflow, self._lead().name))
		self.journeys.append(self._park(self.workflow, self._lead().name))
		enqueued = []
		real_enqueue = frappe.enqueue

		def counting_enqueue(method, **kwargs):
			if "stop_for_workflow" in str(method):
				enqueued.append(method)
			return real_enqueue(method, **kwargs)

		with patch.object(thresholds, "STOP_CHUNK", 2), patch("frappe.enqueue", counting_enqueue):
			self._suspend()

		self.assertEqual(len(enqueued), 1, f"the kill enqueued {len(enqueued)} jobs, not one")
		for journey in self.journeys:
			self._assert_stopped(journey.name, "suspended")

	def test_a_driver_that_wins_the_race_is_not_overwritten(self):
		"""A sweep may be driving a journey the instant the kill reaches it. The loser of that race must
		take NOTHING rather than stop a journey that has already moved on — which is why the row is
		re-checked INSIDE the lock and not merely selected before it."""
		real_persist = interpreter._persist
		raced = []

		def racing_persist(journey, values):
			if not raced:
				# The other driver finishes while this kill holds the row it is on — exactly the window.
				# Whichever row the kill reaches first, the OTHER one is the one that moves under it.
				raced.append(next(j.name for j in self.journeys if j.name != journey.name))
				frappe.db.set_value(
					JOURNEY_DT, raced[0], {"status": "Done", "active_key": None}, update_modified=False
				)
			return real_persist(journey, values)

		with patch.object(interpreter, "_persist", racing_persist):
			stopped = interpreter.stop_for_workflow(_WF, "raced")

		self.assertEqual(stopped, 1, "the kill counted a journey another driver had already finished")
		state = self._state(raced[0])
		self.assertEqual(state.status, "Done", "the kill overwrote a journey that had already moved on")
		self.assertFalse(state.stop_reason, "a half-written row: Done, and carrying a stop reason")

	# ---- the cohort ----------------------------------------------------------------------------

	def test_suspending_aborts_a_cohort_that_is_mid_drain(self):
		"""A drain in flight is still MAKING journeys. Suspending without stopping it races the killer."""
		frappe.db.set_value(WORKFLOW_DT, _WF, {
			"cohort_state": drain.DRAINING, "cohort_abort": 0,
		}, update_modified=False)
		frappe.db.commit()

		self._suspend()

		self.assertEqual(
			frappe.db.get_value(WORKFLOW_DT, _WF, "cohort_abort"), 1,
			"a cohort kept starting journeys into a suspended workflow",
		)

	# ---- coming back ---------------------------------------------------------------------------

	def test_reactivating_starts_fresh_journeys_and_revives_none(self):
		"""SUSPENDED → ACTIVE stays legal: it starts NEW journeys for leads that qualify. There is no
		un-kill, and the freed `active_key` is what lets the same lead enter again."""
		from tatva_connect.workflow_engine import triggers, versions

		self._suspend()
		workflows_api.activate(_WF)

		triggers.start_journey(_WF, versions.current_name(_WF), self.lead.name)

		fresh = frappe.get_all(
			JOURNEY_DT,
			filters={"workflow": _WF, "name": ["not in", [j.name for j in self.journeys]]},
			pluck="name",
		)
		self.assertTrue(fresh, "re-activating started no journey for a lead that qualifies")
		for journey in self.journeys:
			self.assertEqual(
				self._state(journey.name).status, interpreter.STOPPED, "a killed journey was revived"
			)

	# ---- the count the modal shows -------------------------------------------------------------

	def test_the_count_is_the_live_journeys_of_this_workflow(self):
		"""What the confirm modal names. `cohort.preview` cannot answer it — that counts LEADS a scheduled
		trigger would select and returns None for a record-event workflow, which is most of them."""
		frappe.db.set_value(JOURNEY_DT, self.journeys[0].name, "status", "Done", update_modified=False)
		self._park(self.bystander, self.lead.name)
		frappe.db.commit()

		self.assertEqual(workflows_api.live_journey_count(_WF), 1)


class TestDeletingAWorkflowEndsItsJourneys(_KillCase):
	"""Its own workflow, because these tests destroy it. Raised as an adjacent defect, ruled in for W10."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		fixtures.purge(_DOOMED)
		cls.addClassCleanup(fixtures.purge, _DOOMED)
		fixtures.arm_engine(True, cls=cls)
		_arm_sweep(cls)

	def setUp(self):
		self.addCleanup(fixtures.purge, _DOOMED)
		self.workflow = fixtures.make_workflow(_DOOMED, _graph())
		self.journey = self._park(self.workflow, self._lead().name)

	def test_deleting_a_workflow_stops_its_journeys(self):
		"""A workflow that is gone cannot be loaded, so its journeys can never advance again — they sit
		Parked for ever. Reachable only through a FORCED delete: an ordinary one is refused by frappe's own
		link check, which runs AFTER `on_trash` (`delete_doc.py:165` then `:172`)."""
		frappe.delete_doc(WORKFLOW_DT, _DOOMED, force=True, ignore_permissions=True)
		frappe.db.commit()

		self._assert_stopped(self.journey.name, "deleted")

	def test_the_sweep_cannot_drive_a_deleted_workflows_journey(self):
		"""What the orphan actually did, measured rather than assumed.

		The defect was reported as "they error-log on every sweep, because `versions.load` throws". It does
		not: the FROZEN VERSION rows survive a forced delete of their workflow, so `versions.load` succeeds
		and the sweep drives the journey ON — down the graph of a workflow that no longer exists, sending
		whatever it sends. Quieter than an error log, and worse. So this asserts on the journey, not on the
		Error Log table.
		"""
		frappe.delete_doc(WORKFLOW_DT, _DOOMED, force=True, ignore_permissions=True)
		frappe.db.commit()

		wakeups.sweep()

		self._assert_stopped(self.journey.name, "deleted")
		self.assertEqual(
			fixtures.logs(self.journey.name), [],
			"the sweep executed a node for a workflow that no longer exists",
		)


class TestRetiringAWorkflowKillsItTheSameWay(_KillCase):
	"""ARCHIVED is the more final state, so it cannot be softer than Suspended.

	After W10 "Suspended means nothing is in flight" was true and "Archived means nothing is in flight" was
	not — the same lie in a different state name. Both reach the one `end_journeys_in_flight`, never two.

	Rejected, so nobody rebuilds it: making Archive reachable only from Suspended. One line, but it forces
	a two-step retirement for no gain and hides a cleanup rule inside the transition table.
	"""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		fixtures.purge(_RETIRED)
		cls.addClassCleanup(fixtures.purge, _RETIRED)
		fixtures.arm_engine(True, cls=cls)
		_arm_sweep(cls)

	def setUp(self):
		self.addCleanup(fixtures.purge, _RETIRED)
		self.workflow = fixtures.make_workflow(_RETIRED, _graph())
		self.journey = self._park(self.workflow, self._lead().name)

	def test_archiving_an_active_workflow_ends_every_journey(self):
		workflows_api.archive(_RETIRED)

		self._assert_stopped(self.journey.name, "archived")

	def test_nothing_wakes_an_archived_workflows_journeys(self):
		"""The zombie test again, for the other verb. A status column proves the killer wrote a column;
		this proves the two real wake paths find nothing."""
		workflows_api.archive(_RETIRED)

		wakeups.sweep()
		signals.deliver_signal("CRM Lead", self.journey.subject_name, _SIGNAL,
		                      correlation=f"{self.journey.name}::n1")

		self._assert_stopped(self.journey.name, "archived")
		self.assertEqual(fixtures.logs(self.journey.name), [], "a retired workflow executed a node")

	def test_an_archived_workflows_journey_is_not_claimable_even_while_still_parked(self):
		"""The guarantee that makes the kill true at the LIFECYCLE COMMIT rather than when the drain
		finishes — tested without the drain, by archiving the header directly and leaving the row parked."""
		frappe.db.set_value(WORKFLOW_DT, _RETIRED, "lifecycle_state", ARCHIVED, update_modified=False)
		frappe.db.commit()

		wakeups.drive_journey(self.journey.name)

		self.assertEqual(self._state(self.journey.name).status, "Parked", "an archived journey was driven")
		self.assertEqual(fixtures.logs(self.journey.name), [], "an archived workflow executed a node")

	def test_archiving_a_suspended_workflow_is_a_no_op(self):
		"""The journeys are already dead, so the second retirement must find nothing rather than transition
		an already-terminal row a second time — and the first reason must survive it."""
		workflows_api.suspend(_RETIRED)
		self._assert_stopped(self.journey.name, "suspended")

		workflows_api.archive(_RETIRED)

		state = self._state(self.journey.name)
		self.assertEqual(state.status, interpreter.STOPPED)
		self.assertIn("suspended", state.stop_reason, "archiving overwrote the reason the journey ended")

	def test_the_receipt_says_how_many_archiving_stopped(self):
		"""An operator told nothing cannot tell a retirement that worked from one that found no rows."""
		self.assertEqual(workflows_api.archive(_RETIRED)["stopping"], 1)


class TestRevisingLeavesJourneysRunning(_KillCase):
	"""ACTIVE -> DRAFT is an author fixing a typo, not a retirement — and `revise` promises it in its own
	docstring. Journeys run a FROZEN version an edit cannot reach, which is what versions are frozen for."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		fixtures.purge(_REVISED)
		cls.addClassCleanup(fixtures.purge, _REVISED)
		fixtures.arm_engine(True, cls=cls)
		_arm_sweep(cls)

	def setUp(self):
		self.addCleanup(fixtures.purge, _REVISED)
		self.workflow = fixtures.make_workflow(_REVISED, _graph())
		self.journey = self._park(self.workflow, self._lead().name)

	def test_revising_leaves_every_journey_in_flight(self):
		workflows_api.revise(_REVISED)

		state = self._state(self.journey.name)
		self.assertEqual(state.status, "Parked", "reopening the canvas killed a journey")
		self.assertFalse(state.stop_reason, "a running journey was given a stop reason")

	def test_the_frozen_version_still_serves_them(self):
		"""Left alive is only half the promise: the journey has to still be RUNNABLE. Its version is
		asserted to survive and the wake door is asserted to still claim it."""
		version = frappe.db.get_value(JOURNEY_DT, self.journey.name, "workflow_version")

		workflows_api.revise(_REVISED)

		self.assertTrue(frappe.db.exists(fixtures.VERSION_DT, version), "the frozen version went with the revise")
		wakeups.drive_journey(self.journey.name)
		self.assertNotEqual(
			self._state(self.journey.name).status, "Parked",
			"a revised workflow's journey is no longer claimable, so revise has become a soft kill",
		)
