# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Completing a task wakes the run that raised it — and only that one.

The engine parks a run on an outcome and something in the world produces it later. What makes that safe
is CORRELATION: the node that raised the task stamps a token on it, and the Wait that follows correlates
on the same token. Without it the wake is keyed on the lead, so any Done task of any type could advance a
journey waiting on a different task — silently, and looking exactly like correct behaviour.

So the test that matters is the NEGATIVE one: `test_an_unrelated_task_does_not_wake_the_run`. It fires the
same doctype, the same status, on the same lead, and the run must stay parked. A suite that only proves
the happy path cannot tell correlation from a lead-scoped match — which is precisely the bug W6 removed.

Drives the real `doc_events` path: the tasks are saved normally and the wake comes from the wildcard
detector, never from calling `deliver_signal` by hand.
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.tests.authz.grains import assert_masters_exist
from tatva_connect.tests.automation import field_allowlist
from tatva_connect.workflow_engine import interpreter
from tatva_connect.workflow_engine.tests import fixtures as fx

_WORKFLOW = "event-bridge-probe"


class TestEventBridge(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		assert_masters_exist()
		fx.purge(_WORKFLOW)
		cls._was_armed = fx.arm_engine(True)
		cls.lead = fx.make_lead()
		cls.task_type = _task_type()
		cls.workflow = fx.make_workflow(_WORKFLOW, [
			fx.trigger(to="raise"),
			fx.node("raise", "Create Task", config={"task_type": cls.task_type}, edges={"next": "w1"}),
			fx.node("w1", "Wait", config={
				"mode": "Until Event", "source_node": "raise", "event_name": "task.completed",
				"accepts": '{"status": "task_status"}',
			}, edges={"event": "end"}),
			fx.node("end", "Terminal"),
		])
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		fx.arm_engine(bool(cls._was_armed))
		fx.purge(_WORKFLOW)
		from tatva_connect.tests.activity import task_type_fixture as ttf

		field_allowlist.clear("CRM Lead")
		_clear_tasks(cls.lead.name)
		ttf.teardown()
		frappe.delete_doc("CRM Lead", cls.lead.name, force=True, ignore_permissions=True)
		frappe.db.commit()

	def setUp(self):
		self._reset()

	def tearDown(self):
		self._reset()

	def _reset(self):
		for run in frappe.get_all(fx.RUN_DT, filters={"workflow": self.workflow.name}, pluck="name"):
			frappe.db.delete(fx.STEP_LOG_DT, {"workflow_run": run})
		frappe.db.delete(fx.RUN_DT, {"workflow": self.workflow.name})
		frappe.db.delete(fx.EVENT_DT, {"subject_name": self.lead.name})
		_clear_tasks(self.lead.name)
		frappe.db.commit()

	def _park(self):
		"""Run to the Wait. Returns (run_name, the task the Create Task node raised)."""
		run = fx.start_run(self.workflow, self.lead.name, "start")
		interpreter.advance(frappe.get_doc(fx.RUN_DT, run.name))
		frappe.db.commit()
		return run.name, _raised_task(self.lead.name)

	def _complete(self, task):
		"""Mark a task Done through a normal save, so the wildcard detector fires as it does in the app."""
		doc = frappe.get_doc("CRM Task", task)
		doc.status = "Done"
		doc.save(ignore_permissions=True)  # authz-ok: tier-c — test fixture, no user input
		frappe.db.commit()

	def _status(self, run_name):
		return frappe.db.get_value(fx.RUN_DT, run_name, ["status", "current_node"], as_dict=True)

	# --- the token exists at all -----------------------------------------------------------------------

	def test_the_node_stamps_its_token_on_the_task_it_raised(self):
		run_name, task = self._park()
		self.assertTrue(task, "the Create Task node raised no task")
		self.assertEqual(
			frappe.db.get_value("CRM Task", task, "custom_workflow_token"),
			f"{run_name}::raise",
			"the task must carry the run and node that raised it",
		)

	def test_the_wait_correlates_on_that_same_token(self):
		run_name, _raised = self._park()
		row = frappe.db.get_value(
			fx.RUN_DT, run_name, ["status", "awaiting_signal", "awaiting_correlation"], as_dict=True
		)
		self.assertEqual(row.status, "Parked")
		self.assertEqual(row.awaiting_signal, "task.completed")
		self.assertEqual(row.awaiting_correlation, f"{run_name}::raise")

	# --- the wake ---------------------------------------------------------------------------------------

	def test_completing_the_task_emits_the_correlated_outcome(self):
		"""The bridge itself: saving the task the way a rep saves it puts the right event in the inbox.

		Delivery and resumption are deliberately separate — `deliver_signal` writes a durable inbox row
		and enqueues the wake, so a crash between the two loses nothing and the run is woken by the
		worker. This asserts the half that the doc_event is responsible for.
		"""
		run_name, task = self._park()
		self._complete(task)

		events = frappe.get_all(
			fx.EVENT_DT, filters={"subject_name": self.lead.name},
			fields=["event_name", "correlation", "status"],
		)
		self.assertEqual(len(events), 1, "completing the task must emit exactly one outcome")
		self.assertEqual(events[0].event_name, "task.completed")
		self.assertEqual(events[0].correlation, f"{run_name}::raise")

	def test_the_delivered_outcome_resumes_the_run(self):
		"""The other half, driven the way the queue drives it — `resume_for_signal` is the exact function
		`deliver_signal` enqueues, called with the arguments it enqueues."""
		from tatva_connect.workflow_engine import signals

		run_name, task = self._park()
		self._complete(task)
		signals.resume_for_signal(
			"CRM Lead", self.lead.name, "task.completed", correlation=f"{run_name}::raise"
		)
		frappe.db.commit()

		row = self._status(run_name)
		self.assertEqual(row.status, "Done")
		self.assertEqual(row.current_node, "end")
		state = frappe.parse_json(frappe.db.get_value(fx.RUN_DT, run_name, "state_json") or "{}")
		self.assertEqual(
			state.get("w1", {}).get("task_status"), "Done",
			"the outcome's payload merged into state, under the Wait that accepted it",
		)

	# --- the negative that makes the positive mean something --------------------------------------------

	def test_an_unrelated_task_does_not_wake_the_run(self):
		"""Same lead, same doctype, same terminal status — and the run must NOT move.

		This is the bug the token removes: the old detector matched on the lead, so this task would have
		advanced a journey that was waiting on a different one.
		"""
		run_name, _raised = self._park()
		other = frappe.get_doc({
			"doctype": "CRM Task", "title": "Unrelated", "status": "Backlog",
			"reference_doctype": "CRM Lead", "reference_docname": self.lead.name,
			"custom_task_type": self.task_type,
		}).insert(ignore_permissions=True)  # authz-ok: tier-c — test fixture, no user input
		other.status = "Done"
		other.save(ignore_permissions=True)  # authz-ok: tier-c — test fixture, no user input
		frappe.db.commit()
		# Drive the wake too: proving nothing was ENQUEUED is weaker than proving that even a delivered
		# outcome for another task cannot move this run.
		from tatva_connect.workflow_engine import signals

		signals.resume_for_signal("CRM Lead", self.lead.name, "task.completed", correlation=None)
		frappe.db.commit()

		row = self._status(run_name)
		self.assertEqual(row.status, "Parked", "a task this run did not raise must never wake it")
		self.assertEqual(row.current_node, "w1")

	def test_a_task_with_no_token_emits_nothing(self):
		"""A task nobody's workflow raised carries no token, so it correlates to nothing at all."""
		self._park()
		frappe.db.delete(fx.EVENT_DT, {"subject_name": self.lead.name})
		frappe.db.commit()

		plain = frappe.get_doc({
			"doctype": "CRM Task", "title": "Hand made", "status": "Backlog",
			"reference_doctype": "CRM Lead", "reference_docname": self.lead.name,
			"custom_task_type": self.task_type,
		}).insert(ignore_permissions=True)  # authz-ok: tier-c — test fixture, no user input
		plain.status = "Done"
		plain.save(ignore_permissions=True)  # authz-ok: tier-c — test fixture, no user input
		frappe.db.commit()

		self.assertEqual(
			frappe.db.count(fx.EVENT_DT, {"subject_name": self.lead.name}), 0,
			"a task with no workflow token must emit no event",
		)


def _task_type():
	"""The task type the Create Task node raises, minted on the SAME grain as the probe lead.

	Create Task grain-gates every task it raises, so a type on another grain would simply never be
	created and this suite would prove nothing about correlation.
	"""
	from tatva_connect.tests.activity import task_type_fixture as ttf

	return ttf.mint_type(
		"Event Bridge Probe", [],
		vertical=fx.GRAIN["vertical"], group=fx.GRAIN["group"], program=fx.GRAIN["program"],
	)


def _raised_task(lead):
	rows = frappe.get_all(
		"CRM Task", filters={"reference_doctype": "CRM Lead", "reference_docname": lead},
		fields=["name", "custom_workflow_token"], order_by="creation asc",
	)
	tokened = [r.name for r in rows if r.custom_workflow_token]
	return tokened[0] if tokened else None


def _clear_tasks(lead):
	for name in frappe.get_all(
		"CRM Task", filters={"reference_doctype": "CRM Lead", "reference_docname": lead}, pluck="name"
	):
		frappe.delete_doc("CRM Task", name, force=True, ignore_permissions=True)
