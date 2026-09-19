# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""W14 — a signal is stored only when a journey could ever consume it. No dead admissions.

Every delivery receipt, reply and completed task that carried an engine token used to insert a Pending inbox
row and queue a resume, whether or not the journey it named waited on that event. Nothing could take those
rows: they sat Pending until the reaper expired them, and each queued a no-op job on the lane sends and wakes
share. So the door asks first, and the answer is the frozen graph's own Waits.

The negatives are the point — a token for a finished journey, for a graph that only waits on time, for a
node no Wait listens to — and the positives prove the door did not simply close: a Wait that names this
event from this node still gets its row, and a signal with no engine token is admitted exactly as before.

Nothing here sends: the graphs hold no send verb, and the sends gate is never armed.
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.tests.authz.grains import assert_masters_exist
from tatva_connect.workflow_engine import signals
from tatva_connect.workflow_engine.tests import fixtures as fx

_WAITS_ON_TASK = "ZZ Admission Waits On Task"
_WAITS_ON_TIME = "ZZ Admission Waits On Time"
_SIGNAL = "task.completed"


class TestASignalIsStoredOnlyWhenAJourneyCouldTakeIt(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		assert_masters_exist()
		fx.purge(_WAITS_ON_TASK, _WAITS_ON_TIME)
		cls.addClassCleanup(fx.purge, _WAITS_ON_TASK, _WAITS_ON_TIME)
		fx.arm_engine(True, cls=cls)
		from tatva_connect.tests.activity import task_type_fixture as ttf

		task_type = ttf.mint_type(
			"Signal Admission Probe", [], vertical=fx.GRAIN["vertical"], group=fx.GRAIN["group"], program=fx.GRAIN["program"],
		)
		cls.waits_on_task = fx.make_workflow(_WAITS_ON_TASK, [
			fx.trigger(to="raise"),
			fx.node("raise", "Create Task", config={"task_type": task_type}, edges={"next": "w1"}),
			fx.node("w1", "Wait", config={"mode": "Until Event", "source_node": "raise", "event_name": _SIGNAL},
			        edges={"event": "end"}),
			fx.node("end", "Terminal"),
		], lifecycle_state="Published")
		cls.waits_on_time = fx.make_workflow(_WAITS_ON_TIME, [
			fx.trigger(to="raise"),
			fx.node("raise", "Create Task", config={"task_type": task_type}, edges={"next": "w1"}),
			fx.node("w1", "Wait", config={"mode": "For Duration", "duration": "{'days': 2}"}, edges={"next": "end"}),
			fx.node("end", "Terminal"),
		], lifecycle_state="Published")
		cls.lead = fx.make_lead().name
		cls.addClassCleanup(cls._drop_lead)
		frappe.db.commit()

	@classmethod
	def _drop_lead(cls):
		from tatva_connect.tests.activity import task_type_fixture as ttf

		frappe.db.delete(fx.SIGNAL_DT, {"subject_name": cls.lead})
		frappe.delete_doc("CRM Lead", cls.lead, force=True, ignore_permissions=True)
		ttf.teardown()
		frappe.db.commit()

	def setUp(self):
		self.addCleanup(self._reset)

	def _reset(self):
		for workflow in (_WAITS_ON_TASK, _WAITS_ON_TIME):
			frappe.db.delete(fx.JOURNEY_DT, {"workflow": workflow})
		frappe.db.delete(fx.SIGNAL_DT, {"subject_name": self.lead})
		frappe.db.commit()

	def _journey(self, workflow, status="Parked"):
		journey = fx.start_journey(workflow, self.lead, "w1")
		frappe.db.set_value(fx.JOURNEY_DT, journey.name, "status", status, update_modified=False)
		frappe.db.commit()
		return journey.name

	def _deliver(self, correlation):
		signals.deliver_signal("CRM Lead", self.lead, _SIGNAL, correlation=correlation)
		frappe.db.commit()
		return frappe.db.count(fx.SIGNAL_DT, {"subject_name": self.lead, "event_name": _SIGNAL})

	def test_a_wait_on_this_event_from_this_node_still_gets_its_row(self):
		journey = self._journey(self.waits_on_task)

		self.assertEqual(self._deliver(f"{journey}::raise"), 1, "the door refused a signal a Wait is parked for")

	def test_a_journey_still_running_toward_the_wait_gets_its_row(self):
		"""An early signal is kept for the Wait the journey has not reached yet (F1)."""
		journey = self._journey(self.waits_on_task, status="Running")

		self.assertEqual(self._deliver(f"{journey}::raise"), 1, "an early signal was dropped before its Wait")

	def test_a_graph_that_only_waits_on_time_stores_nothing(self):
		"""The prod shape: every receipt for a timer-only flow sat Pending and was never consumed."""
		journey = self._journey(self.waits_on_time)

		self.assertEqual(self._deliver(f"{journey}::raise"), 0, "a signal no Wait listens to was stored")

	def test_a_node_no_wait_listens_to_stores_nothing(self):
		journey = self._journey(self.waits_on_task)

		self.assertEqual(self._deliver(f"{journey}::w1"), 0, "a signal from a node nobody waits on was stored")

	def test_a_finished_journey_stores_nothing(self):
		journey = self._journey(self.waits_on_task, status="Done")

		self.assertEqual(self._deliver(f"{journey}::raise"), 0, "a signal for a finished journey was stored")

	def test_a_token_naming_no_journey_stores_nothing(self):
		self.assertEqual(self._deliver("NO-SUCH-JOURNEY::raise"), 0, "a signal for a journey that does not exist was stored")

	def test_a_signal_with_no_engine_token_is_admitted_as_before(self):
		"""Nothing can say who waits on an uncorrelated signal, so the door leaves it exactly as it was."""
		self.assertEqual(self._deliver(None), 1, "an uncorrelated signal was refused")
