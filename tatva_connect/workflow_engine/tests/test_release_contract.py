# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Publish refuses a graph that cannot run, and freezes the one it accepts.

Two promises were written in docstrings for months and kept by nothing. Publish claimed to run a
"fail-closed release contract"; it ran no checks at all. And publish claimed to freeze an immutable
version; the freeze was lazy, so after a workflow had run once, editing and re-publishing changed
nothing — the author saw a green Published badge while the original graph kept executing.

Both are now real, and this suite is what stops them quietly becoming prose again.

Every rejection case here is a graph made of individually VALID nodes. That is the point: node-level
validation passes all of them, and only a whole-graph view can tell they are broken.
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.tests.authz.grains import assert_masters_exist
from tatva_connect.workflow_engine import graph, registry, versions
from tatva_connect.workflow_engine.tests import fixtures as fx

_WF = "release-contract-probe"


def _graph(*nodes):
	"""The authored shape the validator reads, built from fixture nodes."""
	return [
		{
			"node_id": n["node_id"],
			"node_type": n["node_type"],
			"config_json": frappe.as_json(n.get("config") or {}),
			"edges": [{"from_output": o, "to_node": t} for o, t in (n.get("edges") or {}).items()],
		}
		for n in nodes
	]


class TestGraphRules(FrappeTestCase):
	"""Pure rules — no DB. Each case is a graph of valid nodes that must still be refused."""

	def test_a_sound_graph_has_no_problems(self):
		sound = _graph(
			fx.trigger(to="end"),
			fx.node("end", "Terminal"),
		)
		self.assertEqual(graph.problems(sound, entry_node="start"), [])

	def test_a_graph_with_no_trigger_is_refused(self):
		found = graph.problems(_graph(fx.node("end", "Terminal")))
		self.assertTrue(any("no Trigger" in p["message"] for p in found), found)

	def test_a_route_with_an_unwired_leg_is_refused(self):
		"""The quiet one. It runs correctly until a subject takes the unwired path, then dies."""
		found = graph.problems(_graph(
			fx.trigger(to="b1"),
			fx.node("b1", "Route",
			        config={"routes": [{"id": "r1", "label": "New", "condition": {"type": "rule", "field": "status", "operator": "is", "value": "New"}}]},
			        edges={"r1": "end"}),  # `otherwise` left unwired
			fx.node("end", "Terminal"),
		), entry_node="start")
		self.assertTrue(any("otherwise" in p["message"] for p in found), found)

	def test_an_edge_to_a_deleted_node_is_refused(self):
		found = graph.problems(_graph(
			fx.trigger(to="ghost"),
			fx.node("end", "Terminal"),
		), entry_node="start")
		self.assertTrue(any("ghost" in p["message"] for p in found), found)

	def test_a_graph_that_cannot_end_is_refused(self):
		found = graph.problems(_graph(fx.trigger(to="start")), entry_node="start")
		self.assertTrue(any("never finish" in p["message"] for p in found), found)

	def test_an_unreachable_node_is_refused(self):
		found = graph.problems(_graph(
			fx.trigger(to="end"),
			fx.node("end", "Terminal"),
			fx.node("orphan", "Create Note", config={"comment_mode": "Literal", "comment_text": "hi"},
			        edges={"next": "end"}),
		), entry_node="start")
		self.assertTrue(any("orphan" in p["message"] for p in found), found)

	def test_a_loop_with_no_wait_is_refused(self):
		"""A run would walk it until the hop budget stops it, doing its work over and over."""
		found = graph.problems(_graph(
			fx.trigger(to="a"),
			fx.node("a", "Create Note", config={"comment_mode": "Literal", "comment_text": "hi"},
			        edges={"next": "b"}),
			fx.node("b", "Create Note", config={"comment_mode": "Literal", "comment_text": "ho"},
			        edges={"next": "a"}),
		), entry_node="start")
		self.assertTrue(any("spin" in p["message"] for p in found), found)

	def test_a_loop_through_a_wait_is_allowed(self):
		"""Polling is a legitimate shape — the Wait is what makes it safe."""
		found = graph.problems(_graph(
			fx.trigger(to="w1"),
			fx.node("w1", "Wait", config={"mode": "For Duration", "expression": "{'minutes': 5}"},
			        edges={"next": "b1"}),
			fx.node("b1", "Route",
			        config={"routes": [{"id": "r1", "label": "New", "condition": {"type": "rule", "field": "crm_lead.status", "operator": "is", "value": "New"}}]},
			        edges={"r1": "w1", "otherwise": "end"}),
			fx.node("end", "Terminal"),
		), entry_node="start")
		self.assertEqual(found, [], f"a Wait-guarded loop must be allowed: {found}")

	def test_runs_must_begin_at_the_trigger(self):
		found = graph.problems(_graph(
			fx.trigger(to="end"),
			fx.node("end", "Terminal"),
		), entry_node="end")
		self.assertTrue(any("begin at the Trigger" in p["message"] for p in found), found)


class TestPublish(FrappeTestCase):
	"""The lifecycle, against the real controller and the real version table."""

	@classmethod
	def setUpClass(cls):
		assert_masters_exist()
		fx.purge(_WF)

	@classmethod
	def tearDownClass(cls):
		fx.purge(_WF)
		frappe.db.commit()

	def tearDown(self):
		fx.purge(_WF)

	def _make(self, nodes):
		return fx.make_workflow(_WF, nodes, lifecycle_state="Draft")

	def test_publish_refuses_a_broken_graph(self):
		workflow = self._make([fx.trigger(to="ghost"), fx.node("end", "Terminal")])
		with self.assertRaises(frappe.exceptions.ValidationError) as caught:
			workflow.apply_transition("Published")
		self.assertIn("ghost", str(caught.exception))
		self.assertEqual(
			frappe.db.get_value(fx.WORKFLOW_DT, workflow.name, "lifecycle_state"), "Draft",
			"a refused publish must not move the lifecycle",
		)

	def test_publish_freezes_the_current_graph(self):
		workflow = self._make([fx.trigger(to="end"), fx.node("end", "Terminal")])
		workflow.apply_transition("Published")
		frappe.db.commit()
		self.assertTrue(
			frappe.db.exists(fx.VERSION_DT, {"workflow": workflow.name, "is_current": 1}),
			"publish must freeze a version",
		)

	def test_editing_and_republishing_takes_effect(self):
		"""The defect that made every later edit a no-op: the freeze was lazy, so v1 stayed current for
		ever and the author's changes never reached a run."""
		workflow = self._make([fx.trigger(to="end"), fx.node("end", "Terminal")])
		workflow.apply_transition("Published")
		frappe.db.commit()
		first = versions.current_name(workflow.name)

		workflow.apply_transition("Draft")
		frappe.get_doc({
			"doctype": fx.NODE_DT, "workflow": workflow.name, "node_id": "note", "node_type": "Create Note",
			"sequence": 5, "config_json": frappe.as_json({"comment_mode": "Literal", "comment_text": "hi"}),
			"edges": [{"from_output": "next", "to_node": "end"}],
		}).insert(ignore_permissions=True)  # authz-ok: tier-c — test fixture, no user input
		node = frappe.get_doc(fx.NODE_DT, frappe.get_all(
			fx.NODE_DT, filters={"workflow": workflow.name, "node_type": "Trigger"}, pluck="name")[0])
		node.set("edges", [{"from_output": "next", "to_node": "note"}])
		node.save(ignore_permissions=True)  # authz-ok: tier-c — test fixture, no user input
		workflow.reload()
		workflow.apply_transition("Published")
		frappe.db.commit()

		second = versions.current_name(workflow.name)
		self.assertNotEqual(first, second, "re-publishing an edited graph must mint a new current version")
		frozen = {n["node_id"] for n in versions.load(second).nodes}
		self.assertIn("note", frozen, "the new version must carry the node the author added")


class TestValidationModes(FrappeTestCase):
	"""Shape is checked at every save; completeness only at publish.

	The rule exists because authoring is incremental. Enforcing `reqd` at save refused a draft holding an
	unconfigured Route — and since a Route's condition control has no field catalog to offer yet, that
	made Route a node type which could never be saved at all.

	The pair matters more than either half: moving the check must not DELETE it. So each case below
	asserts both that the draft is accepted and that publish still catches the same fault.
	"""

	def test_a_draft_may_hold_an_unconfigured_node(self):
		problems = registry.validate_node("Route", {}, [], mode=registry.DRAFT)
		self.assertEqual(problems, [], "a required setting left empty is normal while authoring")

	def test_publish_demands_the_same_setting(self):
		problems = registry.validate_node("Route", {}, [], mode=registry.PUBLISH)
		self.assertTrue(any("Routes" in p["message"] for p in problems), problems)

	def test_draft_still_refuses_what_is_wrong_at_any_time(self):
		"""Deferring completeness is not the same as accepting nonsense. A setting the type never
		declared, or a value outside its options, is wrong whatever the author does next."""
		self.assertTrue(
			registry.validate_node("Route", {"not_a_setting": 1}, [], mode=registry.DRAFT),
			"an undeclared setting must be refused even in a draft",
		)
		self.assertTrue(
			registry.validate_node("Trigger", {"event": "Exploded"}, [], mode=registry.DRAFT),
			"a value outside the declared options must be refused even in a draft",
		)
		self.assertTrue(
			registry.validate_node("Terminal", {}, ["nowhere"], mode=registry.DRAFT),
			"an edge on an output the type never declares must be refused even in a draft",
		)

	def test_publish_reports_node_completeness_through_the_graph_contract(self):
		"""The completeness check moved OUT of save must reappear at publish, or it was deleted rather
		than moved — and an unconfigured Route would activate and die on a live lead."""
		found = graph.problems(_graph(
			fx.trigger(to="b1"),
			fx.node("b1", "Route", edges={"otherwise": "end"}),
			fx.node("end", "Terminal"),
		), entry_node="start")
		self.assertTrue(any("Routes" in p["message"] for p in found), found)


class TestProblemShape(FrappeTestCase):
	"""A problem must be usable by the canvas, not merely readable by a human.

	The whole point of the structured shape is that the builder can mark the node at fault. A list of
	sentences can only be toasted — and a toast naming `b1` in a graph of twenty nodes tells the author
	almost nothing. So these assert the DATA, not the wording.
	"""

	def test_a_node_fault_carries_the_node_and_the_field(self):
		found = graph.problems(_graph(
			fx.trigger(to="b1"),
			fx.node("b1", "Route", edges={"otherwise": "end"}),
			fx.node("end", "Terminal"),
		), entry_node="start")
		fault = next((p for p in found if p["node_id"] == "b1"), None)
		self.assertIsNotNone(fault, f"the fault must name the node it belongs to: {found}")
		self.assertEqual(fault["field"], "routes", "and the field on it, so the control can be marked")

	def test_an_unwired_output_names_its_node(self):
		found = graph.problems(_graph(
			fx.trigger(to="b1"),
			fx.node("b1", "Route",
			        config={"routes": [{"id": "r1", "label": "New", "condition": {"type": "rule", "field": "status", "operator": "is", "value": "New"}}]},
			        edges={"r1": "end"}),
			fx.node("end", "Terminal"),
		), entry_node="start")
		self.assertTrue(any(p["node_id"] == "b1" for p in found), found)

	def test_a_graph_level_fault_names_no_node(self):
		"""'This workflow has no Trigger' belongs to the graph, not to any node — so `node_id` is None
		rather than being blamed on an arbitrary box."""
		found = graph.problems(_graph(fx.node("end", "Terminal")))
		self.assertTrue(all(p["node_id"] is None for p in found), found)

	def test_every_problem_has_the_full_shape(self):
		"""A consumer may read any key on any problem without checking whether it exists."""
		found = graph.problems(_graph(
			fx.trigger(to="ghost"),
			fx.node("b1", "Route", edges={}),
			fx.node("end", "Terminal"),
		), entry_node="start")
		self.assertTrue(found)
		for p in found:
			self.assertEqual(set(p), {"node_id", "field", "message", "code", "severity", "fix"}, p)
			self.assertTrue(p["message"], "a problem must always be explainable to a person")
			self.assertIn(p["severity"], (registry.BLOCKS, registry.WARNS))
