# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A journey walks the new graph: Trigger → Step → Branch → Wait → park → resume → Terminal.

Scope is deliberate. W0/W1 rewrote exactly four things in the interpreter — routing by named edge,
reading a node's config out of `config_json`, running a node's own actions, and the Trigger
pass-through. This proves those four, end to end, on a real graph.

It does NOT re-prove what W0 left alone: retry classification, the reconciler's recovery of a dropped
enqueue, inbox dedup, the unique-entry guard. That code is untouched, and its tests come back with the
step that next touches it — W6 for the event path, W4 for actions. Rebuilding them now would mean
rebuilding them twice, because W2, W4 and W6 all still move this model.
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.tests.authz.grains import assert_masters_exist
from tatva_connect.tests.automation import field_allowlist
from tatva_connect.workflow_engine import interpreter, versions
from tatva_connect.workflow_engine.tests import fixtures as fx

_WORKFLOW = "engine-walk-probe"


class TestEngineWalk(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		assert_masters_exist()
		fx.purge(_WORKFLOW)
		# Armed explicitly: the signal path shares the trigger lane's switch, so ambient config must not decide.
		fx.arm_engine(True, cls)
		cls.lead = fx.make_lead()
		cls.workflow = fx.make_workflow(_WORKFLOW, [
			fx.trigger(to="s1"),
			fx.node("s1", "Update Field", edges={"next": "b1"}, config={
				"target_doctype": "CRM Lead",
				"updates": [{"name": "status", "mode": "Literal", "value": "New"}],
			}),
			fx.node("b1", "Route",
			        config={"routes": [{"id": "r1", "label": "taken", "condition": {"type": "rule", "field": "seed.taken", "operator": "is", "value": 0}}]},
			        edges={"r1": "w1", "otherwise": "end"}),
			# No `source_node`: this waits on a signal delivered from OUTSIDE the graph, which is what the
			# resume test sends (`correlation=None`). It used to name `s1`, an Update Field — a node that
			# reports no outcome at all and therefore mints no correlation token, so nothing in the graph
			# could ever have woken this Wait. That is now refused at publish and fails loudly at runtime.
			fx.node("w1", "Wait", config={"mode": "Until Event",
			                              "event_name": "probe.done",
			                              "accepts": '{"outcome": "probe_outcome"}'},
			        edges={"event": "end"}),
			fx.node("end", "Terminal"),
		])
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		fx.purge(_WORKFLOW)
		field_allowlist.clear("CRM Lead")
		frappe.delete_doc("CRM Lead", cls.lead.name, force=True, ignore_permissions=True)
		frappe.db.commit()

	def setUp(self):
		# Seeded, never bypassed: routing around the allowlist would prove the engine works without it.
		field_allowlist.seed_settable(
			"CRM Lead", "status",
			vertical=fx.GRAIN["vertical"], group=fx.GRAIN["group"], program=fx.GRAIN["program"],
		)
		# `active_key` is unique per live Run, and the fixture commits, so rollback cannot clear it.
		self._clear_runs()

	def tearDown(self):
		self._clear_runs()

	def _clear_runs(self):
		for run in frappe.get_all(fx.JOURNEY_DT, filters={"workflow": self.workflow.name}, pluck="name"):
			frappe.db.delete(fx.STEP_LOG_DT, {"journey": run})
		frappe.db.delete(fx.JOURNEY_DT, {"workflow": self.workflow.name})
		frappe.db.delete(fx.SIGNAL_DT, {"subject_name": self.lead.name})
		frappe.db.commit()

	def _run(self):
		run = fx.start_journey(self.workflow, self.lead.name, "start", state={"seed": {"taken": 0}})
		interpreter.advance(frappe.get_doc(fx.JOURNEY_DT, run.name))
		return run

	def _state(self, journey_name):
		return frappe.parse_json(frappe.db.get_value(fx.JOURNEY_DT, journey_name, "state_json") or "{}")

	# --- the four things W0/W1 changed ---------------------------------------------------------------

	def test_a_run_walks_the_graph_and_parks_at_the_wait(self):
		"""Trigger passes through, the Step runs, the Route routes by its named edge, and the Wait parks.

		One test covers all four rewrites because they are links in one chain — if routing by edge name
		were broken the journey would stop at the Trigger, and if config reading were broken the Wait would
		not know it was waiting on an event.
		"""
		run = self._run()
		row = frappe.db.get_value(
			fx.JOURNEY_DT, run.name, ["status", "current_node", "awaiting_signal"], as_dict=True
		)
		self.assertEqual(row.status, "Parked")
		self.assertEqual(row.current_node, "w1")
		self.assertEqual(row.awaiting_signal, "probe.done", "the Wait's event name came from config_json")

		walked = [(entry.node_id, entry.outcome) for entry in fx.logs(run.name)]
		self.assertEqual(
			walked,
			[("start", "ok"), ("s1", "ok"), ("b1", "ok"), ("w1", "parked")],
			"the journey must pass through the Trigger, the Step and the Branch, then park",
		)

	def test_the_route_took_its_first_row_by_name(self):
		"""The routing detail is the OUTPUT name now — the matched row's id — not a column name; that is
		the contract the canvas and the validator share with the interpreter."""
		run = self._run()
		details = {entry.node_id: entry.detail for entry in fx.logs(run.name)}
		self.assertEqual(details["b1"], "r1")

	def test_the_verb_node_ran_its_own_verb(self):
		"""A node IS a verb: its type names what it does and its config is that verb's parameters. The
		lead really carries the value the node wrote — not merely that a handler was called."""
		run = self._run()
		logged = {entry.node_id: entry.outcome for entry in fx.logs(run.name)}
		self.assertEqual(logged["s1"], "ok")
		self.assertEqual(frappe.db.get_value("CRM Lead", self.lead.name, "status"), "New")

	def test_an_event_resumes_the_parked_run_to_terminal(self):
		"""The whole point of parking. The declared payload path merges into state on the way through,
		so a resumed run carries what the event told it."""
		from tatva_connect.workflow_engine import signals

		run = self._run()
		signals.deliver_signal(
			"CRM Lead", self.lead.name, "probe.done", correlation=None, payload={"outcome": "done"}
		)
		frappe.db.commit()

		row = frappe.db.get_value(fx.JOURNEY_DT, run.name, ["status", "current_node"], as_dict=True)
		self.assertEqual(row.status, "Done")
		self.assertEqual(row.current_node, "end")
		self.assertEqual(
			self._state(run.name).get("w1", {}).get("probe_outcome"), "done",
			"the Wait's `accepts` mapping came from config_json and merged the event's payload",
		)

	def test_the_frozen_version_carries_the_graph_and_the_actions(self):
		"""A parked journey executes the graph it started on. Edges and actions are inlined into the version,
		so an edit to the workflow cannot reach a journey already under way."""
		payload = versions.build_payload(self.workflow)
		by_id = {n["node_id"]: n for n in payload["nodes"]}
		self.assertEqual(by_id["b1"]["edges"], [{"output": "otherwise", "to": "end"}, {"output": "r1", "to": "w1"}])
		self.assertEqual(by_id["s1"]["node_type"], "Update Field")
		self.assertEqual(by_id["end"]["edges"], [], "a Terminal declares no outputs")
