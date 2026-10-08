# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A Trigger that also runs without a lead publishes a ticket flow on a call, and refuses what cannot run without one."""
from frappe.tests import IntegrationTestCase

from tatva_connect.workflow_engine import graph, refs
from tatva_connect.workflow_engine.tests import fixtures as fx


def _nodes(without_lead=True, subject="CRM Call Log", lead_node=False):
	"""Trigger → Update Field raising a ticket for the caller → Assign to User giving it to whoever answered → End."""
	trigger = fx.trigger(to="ticket", subject_doctype=subject, grain=False)
	if without_lead:
		trigger["config"]["without_lead"] = 1
	ticket = fx.node("ticket", "Update Field", config={"target_doctype": "HD Ticket", "updates": [
		{"name": "subject", "mode": refs.LITERAL, "value": "Answered call"},
		{"name": "contact", "mode": refs.FROM_CONTEXT, "value": "crm_call_log.from"},
	]}, edges={"next": "note" if lead_node else "assign"})
	note = fx.node("note", "Create Note", config={"note": "called"}, edges={"next": "assign"})
	assign = fx.node("assign", "Assign to User", config={
		"assignee_mode": refs.FROM_CONTEXT, "assignee_variable": "crm_call_log.receiver",
		"target_doctype": "HD Ticket", "assign_mode": "Assign",
	}, edges={"assigned": "end", "nobody": "end"})
	return [trigger, ticket, *([note] if lead_node else []), assign, fx.node("end", "Terminal")]


def _codes(nodes):
	"""(node, code) of every problem the publish gate finds, the fixture's edge map handed over as the gate's rows."""
	authored = [{**n, "edges": [{"from_output": o, "to_node": t} for o, t in n["edges"].items()]} for n in nodes]
	return {(p["node_id"], p["code"]) for p in graph.problems(authored, entry_node="start")}


class TestThePublishGate(IntegrationTestCase):
	def test_a_ticket_flow_on_a_call_publishes(self):
		self.assertFalse({c for c in _codes(_nodes()) if c[1] in ("trigger.without-lead", "node.needs-lead")})

	def test_a_node_that_acts_on_the_lead_is_refused(self):
		self.assertIn(("note", "node.needs-lead"), _codes(_nodes(lead_node=True)))

	def test_unticked_the_same_graph_is_judged_as_before(self):
		self.assertNotIn(("note", "node.needs-lead"), _codes(_nodes(without_lead=False, lead_node=True)))

	def test_a_subject_that_names_no_grain_is_refused(self):
		self.assertIn(("start", "trigger.without-lead"), _codes(_nodes(subject="CRM Task")))
