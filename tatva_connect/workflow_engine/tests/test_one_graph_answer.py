# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""ONE QUESTION ABOUT THE GRAPH, ANSWERED ONCE — `graph_context` is `graph_outputs` + `authoring_context`.

Both took the SAME argument, the full node list, and both derived from it; the canvas never wanted one
without the other, so it fired them back to back on load, after every settled edit and after every save.
`graph_context` asks once. It is a COMPOSITION and this file is what stops it becoming a third answer:

  * each half is compared against the function it came from, called on the same input (never a fixture)
  * the halves are compared as JSON too, so "byte-identical" is asserted rather than claimed
  * the new door is gated exactly like the two it replaces
  * `node_context` still slices `authoring_context`, which must stay independently callable

Run:
    bench --site dev.localhost run-tests --app tatva_connect \\
        --module tatva_connect.workflow_engine.tests.test_one_graph_answer
"""
import json
import unittest

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.workflow_engine import context, registry
from tatva_connect.workflow_engine.tests import fixtures

_SEND = "Send WhatsApp"
_BUTTONS = [{"id": "yes", "label": "Yes"}, {"id": "no", "label": "No"}]


def _node(node_id, node_type, config=None, edges=None):
	return {
		"node_id": node_id,
		"node_type": node_type,
		"config_json": json.dumps(config or {}),
		"edges": [{"from_output": o, "to_node": t} for o, t in (edges or {}).items()],
	}


def _graph():
	"""A graph that exercises BOTH halves: a grain-declaring Trigger, declared rows, and a sibling read."""
	trigger = {
		"subject_doctype": "CRM Lead", "event": "Created",
		"vertical": fixtures.GRAIN["vertical"], "group": fixtures.GRAIN["group"],
		"program": fixtures.GRAIN["program"],
	}
	return [
		_node("trigger-1", registry.TRIGGER, trigger, {"next": "send-1"}),
		_node("send-1", _SEND, {"buttons": _BUTTONS}, {"sent": "wait-1", "failed": "route-1"}),
		_node("wait-1", "Wait", {"mode": registry.UNTIL_EVENT, "source_node": "send-1"}, {"yes": "route-1"}),
		_node("route-1", "Route", {"routes": [{"id": "r1", "label": "A"}]}, {"r1": "end-1", "otherwise": "end-1"}),
		_node("end-1", "Terminal", {}, {}),
	]


def _nodes():
	return json.dumps(_graph())


class TestTheOneAnswerIsTheTwoAnswers(FrappeTestCase):
	"""G21 — parity, against the live functions rather than a recorded shape."""

	def test_each_half_equals_what_its_own_endpoint_returns(self):
		nodes = _nodes()
		answer = context.graph_context(nodes)
		self.assertEqual(answer["outputs"], registry.graph_outputs(nodes))
		self.assertEqual(answer["context"], context.authoring_context(nodes))

	def test_each_half_is_byte_identical_on_the_wire(self):
		nodes = _nodes()
		answer = context.graph_context(nodes)
		self.assertEqual(frappe.as_json(answer["outputs"]), frappe.as_json(registry.graph_outputs(nodes)))
		self.assertEqual(frappe.as_json(answer["context"]), frappe.as_json(context.authoring_context(nodes)))

	def test_it_carries_those_two_halves_and_nothing_else(self):
		self.assertEqual(sorted(context.graph_context(_nodes())), ["context", "outputs"])

	def test_the_halves_are_not_empty_or_the_parity_asserted_nothing(self):
		answer = context.graph_context(_nodes())
		self.assertEqual(sorted(answer["outputs"]), ["end-1", "route-1", "send-1", "trigger-1", "wait-1"])
		self.assertEqual(answer["context"]["subject"], "CRM Lead")

	def test_it_takes_a_list_as_well_as_a_json_string(self):
		"""The canvas posts JSON; a server caller holds the rows. Both existing halves accept either."""
		self.assertEqual(context.graph_context(_graph()), context.graph_context(_nodes()))


class TestTheNewDoorIsGatedLikeTheOldOnes(FrappeTestCase):
	"""G22 — the same read on CRM Workflow both halves already demand."""

	def test_guest_is_refused_by_all_three(self):
		nodes = _nodes()
		self.addCleanup(frappe.set_user, frappe.session.user)
		frappe.set_user("Guest")
		for call in (registry.graph_outputs, context.authoring_context, context.graph_context):
			with self.subTest(call=call.__name__), self.assertRaises(frappe.PermissionError):
				call(nodes)


class TestNodeContextStillSlicesTheGraphAnswer(FrappeTestCase):
	"""G23 — `authoring_context` remains independently callable, because `node_context` is its slice."""

	def test_every_node_still_gets_its_slice(self):
		nodes = _nodes()
		answer = context.authoring_context(nodes)
		for node in _graph():
			node_id = node["node_id"]
			with self.subTest(node=node_id):
				self.assertEqual(context.node_context(nodes, node_id), context.for_node(answer, node_id))

	def test_the_slice_of_the_new_half_is_the_same_slice(self):
		nodes = _nodes()
		half = context.graph_context(nodes)["context"]
		for node in _graph():
			node_id = node["node_id"]
			with self.subTest(node=node_id):
				self.assertEqual(context.node_context(nodes, node_id), context.for_node(half, node_id))


if __name__ == "__main__":
	unittest.main()
