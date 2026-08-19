# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A value picker names journey state, and publish refuses a name nothing produces.

Raised as a suspicion by the control matrix: all seven `value-picker` fields carry NO `check` in
`FIELD_TYPES`, which looked like the most-used control for reading journey state having the weakest gate.
It is not — the gate is declarative. Each of them declares `reads: variable`, `contract.reads_of` turns
that into a reference, and `graph._reference_problems` refuses one that nothing upstream produces.

Locked here because absence of a `check` is what made it look unguarded, and the next reader will draw the
same conclusion: a per-field check and a `reads` declaration are two gates, and counting only the first
undercounts. If a value picker is ever declared without `reads`, this goes red.
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.workflow_engine import graph, registry

_GHOST = "no_such_node.no_such_value"


def _pickers():
	return [
		(node_type, field)
		for node_type in registry.NODE_TYPES
		for field in registry.config_fields(node_type)
		if registry.FIELD_TYPES.get(field.get("type"), {}).get("control") == "value-picker"
	]


class TestEveryValuePickerIsGated(FrappeTestCase):
	def test_there_are_value_pickers_to_judge(self):
		"""A silent zero here would make every assertion below vacuous."""
		self.assertTrue(_pickers())

	def test_each_one_declares_that_it_reads_journey_state(self):
		"""The declaration IS the gate. Without it `reads_of` never sees the value and publish never asks."""
		for node_type, field in _pickers():
			with self.subTest(node=node_type, field=field["name"]):
				self.assertEqual(
					registry.read_kind_of(field), "variable",
					f"{node_type}.{field['name']} is a value picker that declares no read — publish will not check it",
				)

	def test_publish_refuses_a_reference_nothing_produces(self):
		"""The gate driven for real, per field, through the graph the author would publish."""
		for node_type, field in _pickers():
			with self.subTest(node=node_type, field=field["name"]):
				nodes = [
					{"node_id": "trg", "node_type": "Trigger", "edges": [{"from_output": "next", "to_node": "n1"}],
					 "config": {"mode": "Record Event", "subject_doctype": "CRM Lead", "event": "Updated"}},
					{"node_id": "n1", "node_type": node_type, "edges": [], "config": {field["name"]: _GHOST}},
				]
				messages = [
					p["message"] for p in graph.problems(nodes)
					if p.get("node_id") == "n1" and p.get("field") == field["name"]
				]
				self.assertTrue(
					messages,
					f"{node_type}.{field['name']} accepted {_GHOST} — a value picker with no gate",
				)
				self.assertIn(_GHOST, messages[0])
