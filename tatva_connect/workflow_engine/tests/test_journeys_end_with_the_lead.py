# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A lead's journeys end when the lead does.

Two gaps, one behaviour. A parked journey whose lead is DELETED used to stay parked for ever, and the
sweep kept re-driving it against a lead that is gone. A lead whose GRAIN changed carried on down a
journey frozen against a grain it no longer has. The decision is the same for both: stop every journey
that lead has, across all workflows — stopped and gone, never re-routed, because the lead is picked up
by the new grain's workflows the way enrolment already works.

So it is ONE function, `interpreter.stop_for_subject`, with two triggers. Building two stop paths is the
defect this suite is shaped to catch, which is why every test below asserts the same end state however
it was reached.

THE ENGINE SWITCH DOES NOT REACH EITHER TRIGGER. It gates what makes a journey ADVANCE; it never gates
ending one, because ending is cleanup. The re-entrancy and mid-migration guards do still apply — those
close different doors, and neither is a dormancy setting.

STOPPED IS A TERMINAL STATE, NOT A DELETION. The row stays readable and says why it ended; what goes is
its ability to act — `active_key` (so the subject is free to start again), `resume_at` (so no timer wakes
it) and `awaiting_signal`/`awaiting_correlation` (so no delivered signal does either). A test that only
checked `status` would pass on a stop that left the journey wakeable.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \\
        --module tatva_connect.workflow_engine.tests.test_journeys_end_with_the_lead
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.tests.authz.grains import GRAINS
from tatva_connect.workflow_engine import interpreter, triggers, wakeups
from tatva_connect.workflow_engine.tests import fixtures

JOURNEY_DT = fixtures.JOURNEY_DT
_WF_A = "ZZ Journeys End A"
_WF_B = "ZZ Journeys End B"


class TestJourneysEndWithTheLead(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		fixtures.purge(_WF_A, _WF_B)
		fixtures.arm_engine(True, cls=cls)
		cls.addClassCleanup(fixtures.purge, _WF_A, _WF_B)
		cls.workflows = [
			fixtures.make_workflow(name, [fixtures.trigger(to="n1"), fixtures.node("n1", "Terminal")])
			for name in (_WF_A, _WF_B)
		]

	def setUp(self):
		self.lead = fixtures.make_lead()
		self.runs = [self._park(workflow, self.lead.name) for workflow in self.workflows]

	def tearDown(self):
		for name in frappe.get_all(JOURNEY_DT, filters={"workflow": ["in", [_WF_A, _WF_B]]}, pluck="name"):
			frappe.db.delete(fixtures.STEP_LOG_DT, {"journey": name})
		frappe.db.delete(JOURNEY_DT, {"workflow": ["in", [_WF_A, _WF_B]]})
		frappe.db.commit()

	def _park(self, workflow, lead_name):
		"""A journey parked on a timer AND on a signal — every column a stop must clear is populated."""
		run = fixtures.start_journey(workflow, lead_name, "n1")
		frappe.db.set_value(JOURNEY_DT, run.name, {
			"status": "Parked",
			"resume_at": frappe.utils.add_to_date(frappe.utils.now_datetime(), days=-1),
			"awaiting_signal": "document.uploaded",
			"awaiting_correlation": f"{run.name}::n1",
			"active_key": f"{workflow.name}::CRM Lead::{lead_name}",
		}, update_modified=False)
		frappe.db.commit()
		return run

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

	# ---- the lead is deleted ------------------------------------------------------------------
	def test_deleting_a_lead_stops_every_journey_across_every_workflow(self):
		"""Two workflows, two parked journeys, one delete. Neither may be left parked."""
		frappe.delete_doc("CRM Lead", self.lead.name, ignore_permissions=True)
		for run in self.runs:
			self._assert_stopped(run.name, "Lead deleted")

	def test_the_sweep_finds_nothing_to_re_drive_once_the_lead_is_gone(self):
		"""The point of the gap: the sweep kept waking a journey about a lead that no longer exists."""
		due_before = set(wakeups._due_parked())
		for run in self.runs:
			self.assertIn(run.name, due_before, "the fixture must be genuinely due, or this proves nothing")

		frappe.delete_doc("CRM Lead", self.lead.name, ignore_permissions=True)

		due_after = set(wakeups._due_parked())
		for run in self.runs:
			self.assertNotIn(run.name, due_after, f"{run.name} is still due for re-drive after its lead went")

	# ---- the lead changes grain ---------------------------------------------------------------
	def test_changing_a_leads_grain_stops_every_journey(self):
		"""A journey frozen against a grain the lead no longer has must not carry on down it."""
		lead = frappe.get_doc("CRM Lead", self.lead.name)
		lead.custom_vertical = _other_vertical(lead.custom_vertical)
		lead.save(ignore_permissions=True)  # authz-ok: tier-a — test drives the rep's own save path
		for run in self.runs:
			self._assert_stopped(run.name, "Lead grain changed")

	def test_the_stop_reason_names_the_axis_that_moved(self):
		lead = frappe.get_doc("CRM Lead", self.lead.name)
		lead.custom_vertical = _other_vertical(lead.custom_vertical)
		lead.save(ignore_permissions=True)  # authz-ok: tier-a — test drives the rep's own save path
		self.assertIn("custom_vertical", self._state(self.runs[0].name).stop_reason)

	def test_an_ordinary_save_that_leaves_the_grain_alone_stops_nothing(self):
		"""`on_update` fires on EVERY lead save. A stop on a save that moved no axis would end journeys
		every time a rep edited a phone number."""
		lead = frappe.get_doc("CRM Lead", self.lead.name)
		lead.first_name = "ZZ Renamed Not Regrained"
		lead.save(ignore_permissions=True)  # authz-ok: tier-a — test drives the rep's own save path
		for run in self.runs:
			self.assertEqual(self._state(run.name).status, "Parked", "an ordinary edit stopped a journey")

	def test_a_save_with_no_before_image_is_not_a_grain_change(self):
		"""A lead being BORN with a grain is not a lead that MOVED grain — and this suite missed it.

		`has_value_changed` returns True for every field when there is no before-image
		(`document.py:684`), and `is_new()` is already False by the time `on_update` runs inside an
		insert — so the first guard read every CREATION as a grain change and stopped the journey that
		same save had just started. `test_entry_isolation` caught it; this is the lock in the suite that
		owns the behaviour. A freshly loaded doc has no `_doc_before_save`, which is exactly the state an
		insert's `on_update` sees.
		"""
		doc = frappe.get_doc("CRM Lead", self.lead.name)
		self.assertIsNone(doc.get_doc_before_save(), "the fixture must have no before-image, or this proves nothing")

		triggers.on_lead_grain_changed(doc)

		for run in self.runs:
			self.assertEqual(self._state(run.name).status, "Parked", "a lead's creation read as a grain change")

	# ---- the edges ----------------------------------------------------------------------------
	def test_a_journey_that_was_already_terminal_is_untouched(self):
		"""No double transition, no resurrection: a Done journey stays Done and gains no stop reason."""
		done = self.runs[0]
		frappe.db.set_value(JOURNEY_DT, done.name, {"status": "Done", "active_key": None}, update_modified=False)
		frappe.db.commit()

		frappe.delete_doc("CRM Lead", self.lead.name, ignore_permissions=True)

		state = self._state(done.name)
		self.assertEqual(state.status, "Done", "a terminal journey was transitioned a second time")
		self.assertFalse(state.stop_reason, "a Done journey was given a stop reason it never had")
		self._assert_stopped(self.runs[1].name, "Lead deleted")

	def test_another_leads_journeys_are_left_alone(self):
		"""The stop is scoped to ONE subject. A stop that swept by workflow would end a stranger's journey."""
		bystander = fixtures.make_lead()
		bystander_run = self._park(self.workflows[0], bystander.name)

		frappe.delete_doc("CRM Lead", self.lead.name, ignore_permissions=True)

		self.assertEqual(self._state(bystander_run.name).status, "Parked", "a bystander's journey was stopped")

	def test_stopping_twice_changes_nothing(self):
		"""The delete and a grain change can both reach the same journey; the second must be a no-op."""
		first = interpreter.stop_for_subject("CRM Lead", self.lead.name, "first pass")
		second = interpreter.stop_for_subject("CRM Lead", self.lead.name, "second pass")
		self.assertEqual(first, len(self.runs), "the first pass must stop every live journey")
		self.assertEqual(second, 0, "a second pass re-stopped an already-terminal journey")
		self.assertIn("first pass", self._state(self.runs[0].name).stop_reason)

	# ---- the engine switch does not reach a stop ----------------------------------------------
	def test_a_dormant_engine_does_not_keep_a_deleted_leads_journeys_alive(self):
		"""INVERTED, and the module docstring above — "stopped and gone" — was always the decision. A journey
		parked on a lead that no longer exists is neither, however the switch happens to be set."""
		fixtures.arm_engine(False)
		try:
			frappe.delete_doc("CRM Lead", self.lead.name, ignore_permissions=True)
		finally:
			fixtures.arm_engine(True)
		for run in self.runs:
			self._assert_stopped(run.name, "Lead deleted")

	def test_a_dormant_engine_does_not_keep_a_regrained_leads_journeys_alive(self):
		"""The same rule through the other trigger — one behaviour, so one answer about the switch."""
		fixtures.arm_engine(False)
		try:
			lead = frappe.get_doc("CRM Lead", self.lead.name)
			lead.custom_vertical = _other_vertical(lead.custom_vertical)
			lead.save(ignore_permissions=True)  # authz-ok: tier-a — test drives the rep's own save path
		finally:
			fixtures.arm_engine(True)
		for run in self.runs:
			self._assert_stopped(run.name, "Lead grain changed")

	def test_re_arming_the_engine_wakes_none_of_them(self):
		"""What the gate really cost: not merely parked, but left DUE — so the sweep is asked for real."""
		fixtures.arm_engine(False)
		try:
			frappe.delete_doc("CRM Lead", self.lead.name, ignore_permissions=True)
		finally:
			fixtures.arm_engine(True)

		due = set(wakeups._due_parked())
		for run in self.runs:
			self.assertNotIn(run.name, due, f"{run.name} woke on a lead the database has forgotten")


def _other_vertical(current):
	"""A real vertical that is not the lead's — taken from the canonical test grains, never invented.

	`grains.GRAINS` links only to master rows that exist (its own invariant), so this can never stamp a
	lead with a vertical the taxonomy does not have.
	"""
	for candidate in GRAINS:
		if candidate["vertical"] != current:
			return candidate["vertical"]
	raise AssertionError("the test grains carry one vertical — a grain change cannot be driven")
