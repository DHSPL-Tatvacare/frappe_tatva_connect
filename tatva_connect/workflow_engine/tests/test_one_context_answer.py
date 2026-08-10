# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""ONE ANSWER FOR THE GRAPH, SLICED PER NODE — and the slice must equal what the node used to be sent.

`node_context` answered for ONE node and re-shipped the subject's whole schema with it. Measured on the
Anaya activity flow: 241 KB per answer, of which `variables` (162 KB), `settable` (97 KB) and the operator
vocabulary were byte-identical at all 25 nodes — only `emitters` differed, at under 1 KB. The canvas asked
it on every click of a different node, so selecting a box cost a 300ms debounce plus a quarter-megabyte,
and the author watched raw refs redraw as labels.

`authoring_context` answers the same contract once for the graph. This file is what stops the two drifting:

  * the slice is EQUAL to the old per-node answer, for every node of a branching graph
  * the two halves are disjoint, which is what lets the canvas put them back with a plain concat
  * a node the graph does not hold still gets nothing, not the subject's schema

The disjointness matters more than it looks. `refs.py` says collision is impossible by construction — a
node's source is its node id, the subject's is a doctype slug — and `nodeContext.js` relies on exactly
that to hold no rule of its own. Asserted here rather than assumed there.
"""
import json
import unittest

from frappe.tests.utils import FrappeTestCase

from tatva_connect.workflow_engine import context, registry, upstream

_TRIGGER = {"subject_doctype": "CRM Lead", "event": "Created"}


def _node(node_id, node_type, config, edges):
	return {
		"node_id": node_id,
		"node_type": node_type,
		"config_json": json.dumps(config),
		"edges": [{"from_output": out, "to_node": to} for out, to in edges.items()],
	}


def _graph():
	"""An emitting node every downstream node is certain of, then a split that rejoins.

	Both shapes on purpose: the Call API above the Route is what gives the positional half something to
	carry, and the arms that rejoin are where dominance and reachability part company — a node on one arm
	has NOT certainly run at the join, so the slice there must not offer what it wrote.
	"""
	return [
		_node("trigger-1", registry.TRIGGER, _TRIGGER, {"next": "call-api-1"}),
		_node("call-api-1", "Call API", {"capture": []}, {"succeeded": "route-1", "failed": "route-1"}),
		_node("route-1", "Route", {"routes": [{"id": "r1", "label": "A"}]}, {"r1": "task-a", "otherwise": "join-1"}),
		_node("task-a", "Create Task", {"task_type": "x"}, {"next": "join-1"}),
		_node("join-1", "Create Task", {"task_type": "y"}, {"next": "end-1"}),
		_node("end-1", "Terminal", {}, {}),
	]


class TestTheSliceEqualsTheOldAnswer(FrappeTestCase):
	def test_every_node_gets_exactly_what_node_context_sends_it(self):
		"""The regression guard: same graph, same node, same answer — key by key, row by row."""
		graph = _graph()
		answer = context.authoring_context(json.dumps(graph))
		for node in graph:
			node_id = node["node_id"]
			sliced = context.for_node(answer, node_id)
			expected = {
				"subject": answer["subject"],
				"grain": answer["grain"],
				"working_set": answer["working_set"],
				"variables": upstream.available_at(graph, node_id),
				"emitters": upstream.emitters_at(graph, node_id),
				"settable": answer["settable"],
				"operators_by_type": answer["operators_by_type"],
				"operator_shapes": answer["operator_shapes"],
			}
			self.assertEqual(sliced, expected, f"the slice for {node_id} is not the answer it used to get")

	def test_a_node_the_graph_does_not_hold_is_offered_nothing(self):
		"""Not the subject's whole schema attributed to a node that is not there — `available_at`'s own answer."""
		answer = context.authoring_context(json.dumps(_graph()))
		sliced = context.for_node(answer, "no-such-node")
		self.assertEqual(sliced["variables"], [])
		self.assertEqual(sliced["emitters"], [])


class TestTheTwoHalvesAreDisjoint(FrappeTestCase):
	"""What lets `nodeContext.js` concat instead of re-implementing the composition rule."""

	def test_no_emitted_ref_collides_with_a_subject_field(self):
		graph = _graph()
		subject = {f["ref"] for f in upstream.subject_fields_of(graph)}
		self.assertTrue(subject, "the fixture must have a subject or this asserts nothing")
		emitting = 0
		for node in graph:
			emitted = {v["ref"] for v in upstream.emitted_at(graph, node["node_id"])}
			emitting += bool(emitted)
			self.assertEqual(emitted & subject, set(), f"{node['node_id']} would need a de-duplication rule in JS")
		self.assertTrue(emitting, "no node emitted anything, so disjointness was never tested")

	def test_the_halves_put_back_together_are_the_whole(self):
		"""The concat the canvas performs, asserted against the composition the backend owns."""
		graph = _graph()
		for node in graph:
			node_id = node["node_id"]
			emitted = upstream.emitted_at(graph, node_id)
			concat = [*emitted, *upstream.subject_fields_of(graph)]
			self.assertEqual(concat, upstream.available_at(graph, node_id), f"a concat is not enough at {node_id}")


class TestTheSubjectHalfIsAskedOnce(FrappeTestCase):
	"""The point of the whole change: what does not move with position is not sent per node."""

	def test_the_graph_half_is_not_repeated_per_node(self):
		answer = context.authoring_context(json.dumps(_graph()))
		self.assertIn("subject_fields", answer)
		for node_id, positional in answer["nodes"].items():
			self.assertEqual(
				sorted(positional.keys()), ["emitted", "emitters"],
				f"{node_id} carries more than its position — the schema is travelling per node again",
			)


if __name__ == "__main__":
	unittest.main()
