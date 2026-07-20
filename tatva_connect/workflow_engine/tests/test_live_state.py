# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A resumed run reads the subject as it is NOW, not as it was when the workflow started.

Run state used to be captured once, at trigger time, and never refreshed. Every segment after the first
therefore judged a lead that no longer existed: "wait 30 days, then if the lead is still New" tested the
value from 30 days ago, and a WhatsApp template rendered after a Wait used month-old fields — a real
message, to a real patient, built from data the sender would not recognise.

The negative half matters as much: a value a NODE wrote must survive the refresh. Captured variables and
Assign keys are the run's own working memory, and a document field that happens to share a name must not
silently replace them.
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.tests.authz.grains import assert_masters_exist
from tatva_connect.workflow_engine import interpreter
from tatva_connect.workflow_engine.tests import fixtures as fx

_WF = "live-state-probe"


class TestLiveState(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		assert_masters_exist()
		fx.purge(_WF)
		cls._was_armed = fx.arm_engine(True)
		cls.lead = fx.make_lead()
		# Parks on an event, so the run is still alive when the lead changes underneath it.
		cls.workflow = fx.make_workflow(_WF, [
			fx.trigger(to="w1"),
			fx.node("w1", "Wait", config={"mode": "Until Event", "event_name": "probe.go"},
			        edges={"event": "end"}),
			fx.node("end", "Terminal"),
		])
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		fx.arm_engine(bool(cls._was_armed))
		fx.purge(_WF)
		frappe.delete_doc("CRM Lead", cls.lead.name, force=True, ignore_permissions=True)
		frappe.db.commit()

	def setUp(self):
		self._clear()

	def tearDown(self):
		self._clear()

	def _clear(self):
		for run in frappe.get_all(fx.RUN_DT, filters={"workflow": self.workflow.name}, pluck="name"):
			frappe.db.delete(fx.STEP_LOG_DT, {"workflow_run": run})
		frappe.db.delete(fx.RUN_DT, {"workflow": self.workflow.name})
		frappe.db.delete(fx.EVENT_DT, {"subject_name": self.lead.name})
		frappe.db.commit()

	def _park(self, state=None):
		run = fx.start_run(self.workflow, self.lead.name, "start", state=state)
		interpreter.advance(frappe.get_doc(fx.RUN_DT, run.name))
		frappe.db.commit()
		return run.name

	def _seen(self, run_name):
		"""What the engine SEES for this run — the run's own values in front of the subject's live fields.

		Deliberately not `state_json`: the subject's fields are no longer copied into the run at all, so
		asserting on the stored column would test the old design rather than the behaviour that matters.
		"""
		return interpreter._refreshed_state(frappe.get_doc(fx.RUN_DT, run_name))

	def _stored(self, run_name):
		return frappe.parse_json(frappe.db.get_value(fx.RUN_DT, run_name, "state_json") or "{}")

	def _set_lead(self, **values):
		doc = frappe.get_doc("CRM Lead", self.lead.name)
		for key, value in values.items():
			doc.set(key, value)
		doc.save(ignore_permissions=True)  # authz-ok: tier-c — test fixture, no user input
		frappe.db.commit()

	# --- the fix ----------------------------------------------------------------------------------------

	def test_a_resumed_run_sees_the_subject_as_it_is_now(self):
		"""The headline. The lead changes while the run is parked; the next segment must see the change."""
		run_name = self._park()
		self.assertEqual(self._seen(run_name).get("crm_lead.status"), "New")

		self._set_lead(status="Qualified")
		interpreter.advance(frappe.get_doc(fx.RUN_DT, run_name))
		frappe.db.commit()

		self.assertEqual(
			self._seen(run_name).get("crm_lead.status"), "Qualified",
			"a segment must judge the lead as it is now, not as it was when the workflow started",
		)

	def test_a_field_added_after_the_run_started_becomes_readable(self):
		"""A value that was empty at trigger time is not empty for ever."""
		run_name = self._park()
		self._set_lead(mobile_no="9876500222")
		interpreter.advance(frappe.get_doc(fx.RUN_DT, run_name))
		frappe.db.commit()
		self.assertEqual(self._seen(run_name).get("crm_lead.mobile_no"), "9876500222")

	# --- the negative: the run's own memory must survive the refresh ------------------------------------

	def test_a_value_a_node_wrote_is_not_overwritten_by_the_document(self):
		"""A captured variable is the run's working memory. If a document field of the same name replaced
		it on every segment, a Call API's capture would be silently undone by the next Wait."""
		run_name = self._park(state={"n1": {"status": "captured-by-a-node"}})
		interpreter.advance(frappe.get_doc(fx.RUN_DT, run_name))
		frappe.db.commit()
		seen = self._seen(run_name)
		self.assertEqual(
			seen.get("n1.status"), "captured-by-a-node",
			"a node's own value must survive the refresh — it is the run's working memory",
		)
		self.assertEqual(
			seen.get("crm_lead.status"),
			frappe.db.get_value("CRM Lead", self.lead.name, "status"),
			"and it must not have eaten the lead's own status: two sources, two values",
		)

	def test_engine_bookkeeping_survives_a_refresh(self):
		"""`_emitted` carries the correlation a parked Wait will be woken by. Losing it across a segment
		would leave the run unwakeable."""
		run_name = self._park(state={"_engine": {"emitted": {"n1": "tok"}}})
		interpreter.advance(frappe.get_doc(fx.RUN_DT, run_name))
		frappe.db.commit()
		self.assertEqual(self._seen(run_name).get("_engine.emitted"), {"n1": "tok"})

	def test_a_deleted_subject_does_not_crash_the_segment(self):
		"""A lead removed mid-flight must fail at a real read, not while refreshing state. Probed on the
		resolver directly: the Run doctype link-validates `subject_name`, so such a row cannot be
		inserted to reach it through `advance`."""
		orphan = frappe._dict(
			subject_doctype="CRM Lead", subject_name="does-not-exist", state_json='{"n1": {"kept": 1}}'
		)
		state = interpreter._refreshed_state(orphan)
		self.assertEqual(state["n1.kept"], 1, "the run's own values must still be readable")
		self.assertNotIn("crm_lead.status", state, "a vanished subject answers nothing, and does not raise")

	def test_the_subject_is_never_copied_into_the_run(self):
		"""The structural guarantee behind all of the above. If a field were stored on the run, a later
		segment would read the copy instead of the document, and the copy is what goes stale."""
		run_name = self._park()
		stored = self._stored(run_name)
		self.assertNotIn(
			"crm_lead", stored,
			f"the subject's fields must not be persisted on the run: {stored}",
		)
