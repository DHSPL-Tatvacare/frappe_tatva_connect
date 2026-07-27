# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""ROUTE — N branches on a condition, first match wins, plus an automatic Otherwise. Branch is deleted.

A real journey fans out more than two ways: a disposition is one of five, each picking a different next
step. That needed five stacked Branch nodes. Route is ONE node — rows of (label, condition), tried top to
bottom, first match wins — with an `otherwise` that is reserved and cannot be forgotten, so a lead can
NEVER fall out of the graph. Branch is deleted (G5): it was Route with one row.

Outputs derive from the row list through the SAME `outputs_by.rows_from` seam Wait uses, reading this
node's OWN config instead of a sibling's — one resolver, one answer. Validation is FREE: W2.2's table
plus the publish gate refuse an unwired output and a row whose condition reads a value nothing upstream
produces, with no new validator. Rows are `{id, label, condition}` — the SAME shape as a Wait button, so
a disposition can be renamed without stranding the edge wired to it.
"""
import json
import unittest

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.tests.authz.grains import assert_masters_exist
from tatva_connect.workflow_engine import graph, interpreter, registry
from tatva_connect.workflow_engine.tests import fixtures as fx

_WF = "route-node-probe"


def _row(rid, label, field, op, value):
	return {"id": rid, "label": label,
	        "condition": {"type": "rule", "field": field, "operator": op, "value": value}}


def _node(node_id, node_type, config=None, edges=None):
	return {
		"node_id": node_id, "node_type": node_type, "config_json": json.dumps(config or {}),
		"edges": [{"from_output": o, "to_node": t} for o, t in (edges or {}).items()],
	}


def _trigger(to="rt"):
	return _node("start", "Trigger", {
		"subject_doctype": "CRM Lead", "event": "Created",
		"vertical": fx.GRAIN["vertical"], "group": fx.GRAIN["group"], "program": fx.GRAIN["program"],
	}, {"next": to})


def _messages(nodes):
	return " | ".join(p["message"] for p in graph.problems(nodes, entry_node="start"))


class TestRouteOutputsAreItsOwnRowsPlusOtherwise(unittest.TestCase):
	"""The seam, read from OWN config. One handle per row, then a reserved `otherwise`."""

	def test_outputs_are_one_per_row_then_otherwise(self):
		outs = registry.outputs_for("Route", {"routes": [{"id": "a"}, {"id": "b"}, {"id": "c"}]})
		self.assertEqual(outs, ["a", "b", "c", "otherwise"])

	def test_no_rows_is_just_otherwise_no_placeholder_leaks(self):
		self.assertEqual(registry.outputs_for("Route", {"routes": []}), ["otherwise"])
		self.assertEqual(registry.outputs_for("Route", {}), ["otherwise"])

	def test_a_row_without_an_id_draws_no_handle(self):
		outs = registry.outputs_for("Route", {"routes": [{"id": "a"}, {"label": "half-typed"}]})
		self.assertEqual(outs, ["a", "otherwise"])


class TestTheUnifiedResolverStillSplicesAWait(FrappeTestCase):
	"""② the regression that matters: Route did not get its own resolver — the ONE `outputs_for` was
	generalised. A Wait reading a SIBLING's buttons must still splice into its mode-map exactly as before."""

	def test_a_wait_still_derives_outputs_from_a_sibling(self):
		graph_config = {"send-1": {"buttons": [{"id": "yes"}, {"id": "no"}]}}
		outs = registry.outputs_for("Wait", {"mode": registry.UNTIL_EVENT, "source_node": "send-1"}, graph_config)
		self.assertIn("yes", outs)
		self.assertIn("no", outs)
		self.assertNotIn("event", outs, "the buttons replace the plain event leg, as before")


class TestTheRoutesFieldReshapesTheHandles(FrappeTestCase):
	"""The routes field keys the node's OWN outputs, so the inspector must re-resolve handles when a row is
	added or removed — `shapes_outputs` marks it, the same flag Wait's `mode` carries. A Wait reading a
	SIBLING's buttons is not shaped by a field of its own, and must stay false."""

	def _wired(self, node_type, field_name):
		payload = next(n for n in registry.node_types() if n["type"] == node_type)
		return next(f for f in payload["config"] if f["name"] == field_name)

	def test_the_routes_field_shapes_outputs(self):
		self.assertTrue(self._wired("Route", "routes")["shapes_outputs"])

	def test_a_waits_mode_still_shapes_outputs(self):
		self.assertTrue(self._wired("Wait", "mode")["shapes_outputs"])

	def test_a_waits_source_node_is_not_shaped_by_a_field_of_its_own(self):
		self.assertFalse(self._wired("Wait", "source_node")["shapes_outputs"])


class TestBranchIsDeleted(unittest.TestCase):
	"""G5 — two ways to do one thing is the defect class this project removes."""

	def test_branch_is_not_a_node_type(self):
		self.assertNotIn("Branch", registry.NODE_TYPES)

	def test_asking_for_the_branch_declaration_throws(self):
		with self.assertRaises(frappe.ValidationError):
			registry.declaration("Branch")


class TestRouteTakesExactlyOneBranchByFirstMatch(FrappeTestCase):
	"""Runtime, through the real interpreter. The outcome asserted is the node the run finished on."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		assert_masters_exist()
		fx.purge(_WF)
		fx.arm_engine(True, cls)
		cls.lead = fx.make_lead()
		cls.workflow = fx.make_workflow(_WF, [
			fx.trigger(to="rt"),
			fx.node("rt", "Route", config={"routes": [
				_row("r_new", "New", "crm_lead.status", "is", "New"),
				_row("r_qual", "Qualified", "crm_lead.status", "is", "Qualified"),
			]}, edges={"r_new": "end_new", "r_qual": "end_qual", "otherwise": "end_other"}),
			fx.node("end_new", "Terminal"),
			fx.node("end_qual", "Terminal"),
			fx.node("end_other", "Terminal"),
		])
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		fx.purge(_WF)
		frappe.delete_doc("CRM Lead", cls.lead.name, force=True, ignore_permissions=True)
		frappe.db.commit()
		super().tearDownClass()

	def _walk_with_status(self, status):
		frappe.db.set_value("CRM Lead", self.lead.name, "status", status)
		frappe.db.commit()
		run = fx.start_run(self.workflow, self.lead.name, "start")
		interpreter.advance(frappe.get_doc(fx.RUN_DT, run.name))
		result = frappe.get_doc(fx.RUN_DT, run.name)
		frappe.db.delete(fx.STEP_LOG_DT, {"workflow_run": run.name})
		frappe.db.delete(fx.RUN_DT, {"name": run.name})
		frappe.db.commit()
		return result

	def test_the_first_matching_row_is_taken(self):
		self.assertEqual(self._walk_with_status("New").current_node, "end_new")

	def test_a_lower_row_matches_when_the_first_does_not(self):
		self.assertEqual(self._walk_with_status("Qualified").current_node, "end_qual")

	def test_an_unmatched_lead_takes_otherwise(self):
		"""THE promise: a lead matching no row does not fall out of the graph — Otherwise catches it."""
		self.assertEqual(self._walk_with_status("Junk").current_node, "end_other")


class TestPublishRefusesAnUnwiredOrUnproducibleRoute(FrappeTestCase):
	"""Validation is FREE — the existing publish rules fire on Route with no new validator code."""

	def test_an_unwired_row_output_is_refused(self):
		nodes = [
			_trigger(to="rt"),
			_node("rt", "Route", {"routes": [
				_row("a", "A", "crm_lead.status", "is", "New"),
				_row("b", "B", "crm_lead.status", "is", "Junk"),
			]}, {"a": "end", "otherwise": "end"}),  # row "b" is unwired
			_node("end", "Terminal"),
		]
		self.assertIn("b", _messages(nodes))

	def test_a_fully_wired_route_publishes_clean(self):
		nodes = [
			_trigger(to="rt"),
			_node("rt", "Route", {"routes": [_row("a", "A", "crm_lead.status", "is", "New")]},
			      {"a": "end", "otherwise": "end"}),
			_node("end", "Terminal"),
		]
		self.assertNotIn("has nothing connected", _messages(nodes))

	def test_a_row_condition_reading_a_value_nothing_produces_is_refused(self):
		"""The predicate_rows reads-kind: a row testing `ghost.value` — no node produces it — is refused at
		publish, exactly as a single Branch predicate would be."""
		nodes = [
			_trigger(to="rt"),
			_node("rt", "Route", {"routes": [_row("a", "A", "ghost.value", "is", "X")]},
			      {"a": "end", "otherwise": "end"}),
			_node("end", "Terminal"),
		]
		self.assertIn("ghost.value", _messages(nodes))
