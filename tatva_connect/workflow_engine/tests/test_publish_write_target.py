# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Update Field's write target and field are judged at PUBLISH, not only on the first live lead.

`_action_set_field` fails closed at runtime and `_resolve_write_target` raises on an out-of-scope doctype,
which is correct — and invisible. `validate_node` skipped `Target` outright and `Field` carries no options
list, so a workflow naming a misspelt field or a doctype the run cannot reach published green and then
died `_Permanent` on a real record. That is the failure class `graph.py` exists to remove.

The publish check is deliberately WEAKER than the runtime one, and this suite proves the split: membership
in the can_set catalog is asked here (`fields.is_set_declared`), the grain-specific decision stays at
execution. At publish there is no lead — only the workflow's declared RULE grain, whose blank axis means
ANY, and feeding that to `is_settable` would answer confidently wrong.
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.automation import fields
from tatva_connect.tests.automation import field_allowlist
from tatva_connect.workflow_engine import graph
from tatva_connect.workflow_engine.tests import fixtures as fx

_SETTABLE = "status"


def _graph(config):
	"""Trigger → the Update Field under test → End. Everything else about the graph is valid, so any
	problem this suite sees is the one it asked about."""
	return [
		{"node_id": "start", "node_type": "Trigger",
		 "config": {"subject_doctype": "CRM Lead", "event": "Created"},
		 "edges": [{"from_output": "next", "to_node": "u1"}]},
		{"node_id": "u1", "node_type": "Update Field", "config": config,
		 "edges": [{"from_output": "next", "to_node": "end"}]},
		{"node_id": "end", "node_type": "Terminal", "edges": []},
	]


def _on(problems, node_id, field):
	return [p for p in problems if p["node_id"] == node_id and p["field"] == field]


class TestPublishWriteTarget(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		field_allowlist.seed_settable(
			"CRM Lead", _SETTABLE,
			vertical=fx.GRAIN["vertical"], group=fx.GRAIN["group"], program=fx.GRAIN["program"],
		)
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		field_allowlist.clear("CRM Lead")
		frappe.db.commit()

	def test_a_field_outside_the_allowlist_is_refused_at_publish(self):
		"""The misspelling. `custom_nonexistent_field` is on no catalog row, so nothing may ever set it."""
		problems = graph.problems(_graph({
			"target_doctype": "CRM Lead", "fieldname": "custom_nonexistent_field",
			"value_mode": "Literal", "value": "x",
		}), entry_node="start")
		self.assertTrue(_on(problems, "u1", "fieldname"), "publish accepted a field automation may never set")

	def test_an_allowlisted_field_publishes(self):
		"""The other direction. A gate that refused every field would pass the check above."""
		problems = graph.problems(_graph({
			"target_doctype": "CRM Lead", "fieldname": _SETTABLE,
			"value_mode": "Literal", "value": "New",
		}), entry_node="start")
		self.assertEqual(_on(problems, "u1", "fieldname"), [], problems)

	def test_a_target_the_run_cannot_reach_is_refused_at_publish(self):
		"""`_resolve_write_target` raises for anything that is neither the Lead nor the trigger doc, and the
		trigger doc's doctype IS the subject. Refused here rather than as a dead run."""
		problems = graph.problems(_graph({
			"target_doctype": "CRM Task", "fieldname": _SETTABLE,
			"value_mode": "Literal", "value": "New",
		}), entry_node="start")
		self.assertTrue(_on(problems, "u1", "target_doctype"), "publish accepted an unreachable write target")

	def test_the_subject_is_a_reachable_target(self):
		"""A Task-subject workflow may write onto the Task that fired it — that is the trigger doc."""
		nodes = _graph({"target_doctype": "CRM Task", "fieldname": "status",
		                "value_mode": "Literal", "value": "Done"})
		nodes[0]["config"]["subject_doctype"] = "CRM Task"
		problems = graph.problems(nodes, entry_node="start")
		self.assertEqual(_on(problems, "u1", "target_doctype"), [], problems)

	def test_publish_asks_membership_and_never_a_rule_grain(self):
		"""The split, stated as a test. `is_set_declared` takes NO axes: there is no data grain at publish,
		and the workflow's declared grain is a rule grain whose blank axis means ANY."""
		import inspect

		self.assertNotIn("axes", inspect.signature(fields.is_set_declared).parameters)
		self.assertTrue(fields.is_set_declared("CRM Lead", _SETTABLE))
		self.assertFalse(fields.is_set_declared("CRM Lead", "custom_nonexistent_field"))
