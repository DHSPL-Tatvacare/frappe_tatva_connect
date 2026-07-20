# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Run history — the read surface's four contracts.

SCOPING is the one that matters. A run carries its lead's field values in `state_json` and a step
detail quotes them, so the endpoints are patient-data surfaces. Two REAL Sales Users are used, not
mocks: crm scopes a lead to its `lead_owner` (or an assignee), so `rep_b` genuinely cannot read
`rep_a`'s lead, and the gate is exercised through `frappe.has_permission` exactly as production
reaches it.

The other three: steps come back in EXECUTION order, every query is bounded however large a limit
the caller asks for, and a parked run reports what it is waiting on — including whether the signal
it named is actually buffered, which is the difference between a park that will end and one that
will not.

Run:
    bench --site dev.localhost run-tests --module tatva_connect.workflow_engine.tests.test_run_history
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.workflow_engine import history
from tatva_connect.workflow_engine.tests import fixtures

WORKFLOW = "WF History Probe"
REP_A = "history.repa@example.test"
REP_B = "history.repb@example.test"


def _rep(email):
	if not frappe.db.exists("User", email):
		user = frappe.get_doc({
			"doctype": "User", "email": email, "first_name": email.split("@")[0],
			"send_welcome_email": 0, "roles": [{"role": "Sales User"}],
		}).insert(ignore_permissions=True)
		return user.name
	return email


class TestRunHistory(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		fixtures.purge(WORKFLOW)
		_rep(REP_A)
		_rep(REP_B)
		cls.workflow = fixtures.make_workflow(WORKFLOW, [
			fixtures.trigger(to="end"),
			fixtures.node("end", "Terminal"),
		])
		cls.lead = fixtures.make_lead()
		# crm scopes a lead to its owner: this is what puts the lead on rep_a's line and not rep_b's.
		frappe.db.set_value("CRM Lead", cls.lead.name, "lead_owner", REP_A)
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		"""setUpClass commits (a run must be durable before the entry segment will retry it), so the
		rollback FrappeTestCase gives us does not undo any of this — every row it created is removed
		by hand or it is left on the bench."""
		frappe.set_user("Administrator")
		fixtures.purge(WORKFLOW)
		frappe.delete_doc("CRM Lead", cls.lead.name, force=True, ignore_permissions=True)
		for email in (REP_A, REP_B):
			if frappe.db.exists("User", email):
				frappe.delete_doc("User", email, force=True, ignore_permissions=True)
		frappe.db.commit()
		super().tearDownClass()

	def tearDown(self):
		frappe.set_user("Administrator")
		super().tearDown()

	def _run(self, **values):
		run = fixtures.start_run(self.workflow, self.lead.name, "end")
		if values:
			frappe.db.set_value(fixtures.RUN_DT, run.name, values)
			frappe.db.commit()
		return frappe.db.get_value(fixtures.RUN_DT, run.name, "name")

	def _step(self, run, node_id, outcome, detail="", duration_ms=0):
		return frappe.get_doc({
			"doctype": fixtures.STEP_LOG_DT, "workflow_run": run, "subject_name": self.lead.name,
			"node_id": node_id, "node_type": "Branch", "outcome": outcome,
			"detail": detail, "duration_ms": duration_ms,
		}).insert(ignore_permissions=True).name

	# ---------------------------------------------------------------- scoping

	def test_rep_on_another_line_sees_nothing(self):
		run = self._run()
		frappe.set_user(REP_B)
		with self.assertRaises(frappe.DoesNotExistError):
			history.runs_for_subject("CRM Lead", self.lead.name)
		with self.assertRaises(frappe.DoesNotExistError):
			history.run_steps(run)
		with self.assertRaises(frappe.DoesNotExistError):
			history.run_state(run)

	def test_rep_on_the_line_sees_the_run(self):
		run = self._run()
		frappe.set_user(REP_A)
		names = [r["run"] for r in history.runs_for_subject("CRM Lead", self.lead.name)["runs"]]
		self.assertIn(run, names)
		self.assertEqual(history.run_state(run)["run"], run)

	def test_missing_and_out_of_scope_are_indistinguishable(self):
		run = self._run()
		frappe.set_user(REP_B)
		with self.assertRaises(frappe.DoesNotExistError) as foreign:
			history.run_state(run)
		with self.assertRaises(frappe.DoesNotExistError) as absent:
			history.run_state("WF-RUN-DOES-NOT-EXIST")
		self.assertEqual(str(foreign.exception), str(absent.exception))

	def test_stuck_runs_hides_a_run_whose_subject_is_unreadable(self):
		"""The operator view drops a run the caller cannot read the subject of — proven with a subject
		that resolves for NOBODY (its lead is gone), because the alternative principal, a Sales Manager
		outside the hierarchy, is unscoped by crm's own rule and would prove nothing."""
		mine = self._run(status="Failed")
		orphan = self._run(status="Failed")
		frappe.db.set_value(fixtures.RUN_DT, orphan, "subject_name", "CRM-LEAD-DELETED-ZZZ")
		frappe.db.commit()

		listed = [r["run"] for r in history.stuck_runs(limit=100)["runs"]]
		self.assertIn(mine, listed)
		self.assertNotIn(orphan, listed, "a run whose subject cannot be read must not be listed")

	def test_stuck_runs_is_not_a_rep_surface(self):
		self._run(status="Failed")
		frappe.set_user(REP_B)
		# rep_b holds no CRM Workflow Run DocPerm at all — the operator view is gated before it reads.
		with self.assertRaises(frappe.PermissionError):
			history.stuck_runs()

	# ---------------------------------------------------------------- ordering

	def test_steps_come_back_in_execution_order(self):
		run = self._run()
		written = [self._step(run, f"n{i}", "ok", detail=f"step {i}") for i in range(5)]
		frappe.db.commit()
		steps = history.run_steps(run)["steps"]
		self.assertEqual([s.name for s in steps], written)
		self.assertEqual([s.node_id for s in steps], ["n0", "n1", "n2", "n3", "n4"])

	# ---------------------------------------------------------------- boundedness

	def test_a_caller_cannot_lift_the_limit(self):
		run = self._run()
		for i in range(4):
			self._step(run, f"n{i}", "ok")
		frappe.db.commit()
		self.assertLessEqual(len(history.run_steps(run, limit=10_000)["steps"]), history.MAX_STEPS)
		self.assertEqual(history._bounded(10_000, history.MAX_STEPS), history.MAX_STEPS)
		self.assertEqual(history._bounded(0, history.MAX_STEPS), history.MAX_STEPS)
		self.assertEqual(history._bounded("nonsense", history.MAX_STEPS), history.MAX_STEPS)
		self.assertEqual(history._bounded(-5, history.MAX_STEPS), 1)

	def test_a_page_reports_that_more_remain(self):
		run = self._run()
		for i in range(3):
			self._step(run, f"n{i}", "ok")
		frappe.db.commit()
		page = history.run_steps(run, limit=2)
		self.assertEqual(len(page["steps"]), 2)
		self.assertTrue(page["has_more"])
		self.assertFalse(history.run_steps(run, limit=2, start=2)["has_more"])

	# ---------------------------------------------------------------- why it stopped

	def test_a_parked_run_reports_the_signal_it_waits_on(self):
		run = self._run(status="Parked", awaiting_signal="task_done", awaiting_correlation="tok-1", resume_at=None)
		waiting = history.run_state(run)["waiting_on"]
		self.assertEqual(waiting["signal"], "task_done")
		self.assertEqual(waiting["correlation"], "tok-1")
		self.assertFalse(waiting["signal_pending"], "nothing is buffered yet")

		frappe.get_doc({
			"doctype": fixtures.EVENT_DT, "subject_doctype": "CRM Lead", "subject_name": self.lead.name,
			"event_name": "task_done", "correlation": "tok-1", "status": "Pending",
		}).insert(ignore_permissions=True)
		frappe.db.commit()
		self.assertTrue(history.run_state(run)["waiting_on"]["signal_pending"])

	def test_a_parked_run_reports_the_clock_it_waits_on(self):
		when = frappe.utils.add_to_date(None, days=3)
		run = self._run(status="Parked", resume_at=when, awaiting_signal=None)
		state = history.run_state(run)
		self.assertEqual(frappe.utils.get_datetime(state["waiting_on"]["resume_at"]), frappe.utils.get_datetime(when))
		self.assertFalse(state["stuck"], "a park with a clock will end on its own")

	def test_a_finished_run_waits_on_nothing(self):
		self.assertIsNone(history.run_state(self._run(status="Done"))["waiting_on"])

	def test_the_failure_reason_is_derived_from_the_last_failed_step(self):
		run = self._run(status="Failed")
		self._step(run, "n1", "ok", detail="fine")
		self._step(run, "n2", "failed", detail="first blow-up")
		self._step(run, "n3", "failed", detail="the one that stuck")
		frappe.db.commit()
		failure = history.run_state(run)["failure"]
		self.assertEqual(failure["detail"], "the one that stuck")
		self.assertEqual(failure["node_id"], "n3")

	def test_a_step_that_failed_is_not_a_run_that_failed(self):
		"""A run can carry a `failed` step and still finish: a verb that routes on its own result leaves
		by a failure edge and the graph carries on. The reason is reported for a FAILED RUN, never for
		any run that happens to have a failed step, or every recovered run reads as broken."""
		run = self._run(status="Done")
		self._step(run, "n1", "failed", detail="the send bounced, the graph took the other edge")
		self._step(run, "n2", "done")
		frappe.db.commit()
		self.assertIsNone(history.run_state(run)["failure"])
		self.assertFalse(history.run_state(run)["stuck"])

	# ---------------------------------------------------------------- what is stuck

	def test_stuck_is_failed_or_a_park_with_nothing_to_wake_it(self):
		failed = self._run(status="Failed")
		orphaned = self._run(status="Parked", resume_at=None, awaiting_signal=None)
		on_a_clock = self._run(status="Parked", resume_at=frappe.utils.add_to_date(None, days=1), awaiting_signal=None)
		on_a_signal = self._run(status="Parked", resume_at=None, awaiting_signal="task_done")
		running = self._run(status="Running")

		self.assertTrue(history.run_state(failed)["stuck"])
		self.assertTrue(history.run_state(orphaned)["stuck"])
		self.assertFalse(history.run_state(on_a_clock)["stuck"])
		self.assertFalse(history.run_state(on_a_signal)["stuck"], "a named signal can still arrive")
		self.assertFalse(history.run_state(running)["stuck"])

		listed = [r["run"] for r in history.stuck_runs(limit=100)["runs"]]
		self.assertIn(failed, listed)
		self.assertIn(orphaned, listed)
		self.assertNotIn(on_a_clock, listed)
		self.assertNotIn(on_a_signal, listed)
		self.assertNotIn(running, listed)

	def test_nothing_here_writes(self):
		run = self._run(status="Failed")
		self._step(run, "n1", "failed", detail="boom")
		frappe.db.commit()
		before = frappe.db.get_value(fixtures.RUN_DT, run, ["status", "modified", "current_node"], as_dict=True)
		history.run_state(run)
		history.run_steps(run)
		history.runs_for_subject("CRM Lead", self.lead.name)
		history.stuck_runs()
		after = frappe.db.get_value(fixtures.RUN_DT, run, ["status", "modified", "current_node"], as_dict=True)
		self.assertEqual(before, after)
