# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A node stores the settings its panel SHOWS, and nothing else.

A gated field keeps its value after its gate shuts: pick Expression on Create Note, write one, switch to
Literal, and the expression is still in the config with no box on screen. `contract.reads_of` walks every
DECLARED field, so publish then refuses on a reference the author cannot reach — the panel says four boxes,
the config remembers six, and there is no way out of that from the UI.

Saved is what was shown. The author loses nothing while editing, because the canvas holds the whole object
and toggling a mode back finds the value still there; only the WRITE projects. `registry.applied_fields` is
the one reader of the gate, so the panel and the stored row cannot disagree about which boxes exist.
"""
import json

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.workflow_engine import contract, registry

_WF = "ZZ Saved Config Projection"


def _node(workflow, node_id, node_type, config):
	return frappe.get_doc({
		"doctype": "CRM Workflow Node", "workflow": workflow, "node_id": node_id,
		"node_type": node_type, "sequence": 1, "config_json": json.dumps(config),
	}).insert(ignore_permissions=True)


def _stored(node):
	return json.loads(frappe.db.get_value("CRM Workflow Node", node.name, "config_json"))


class TestSavedConfigIsWhatThePanelShows(FrappeTestCase):
	def setUp(self):
		self.workflow = frappe.get_doc({
			"doctype": "CRM Workflow", "workflow_name": _WF, "lifecycle_state": "Draft",
		}).insert(ignore_permissions=True).name
		self.addCleanup(frappe.db.rollback)

	def test_a_field_whose_gate_is_shut_is_not_stored(self):
		"""The defect itself: an author switches mode and the old box's value survives off screen."""
		node = _node(self.workflow, "a1", "Assign to User", {
			"assignee_mode": "User", "assign_to_user": "Administrator", "assign_mode": "Assign",
			"assignee_variable": "n2.some_value",
		})
		self.assertNotIn("assignee_variable", _stored(node))

	def test_the_stored_row_equals_the_panel_exactly(self):
		"""Not 'fewer keys' — the SAME keys. Anything else is a second answer to what a node is made of."""
		config = {
			"assignee_mode": "From Variable", "assignee_variable": "crm_lead.lead_owner",
			"assign_to_user": "Administrator", "assign_mode": "Assign", "assign_note": "why",
		}
		stored = _stored(_node(self.workflow, "a2", "Assign to User", config))
		shown = {f["name"] for f in registry.applied_fields("Assign to User", stored)}
		self.assertEqual(set(stored), shown)

	def test_publish_can_no_longer_read_a_box_the_author_cannot_see(self):
		"""The consequence that blocked a real workflow: a stale reference the gate demanded and the panel
		never offered a way to clear."""
		config = {"assignee_mode": "User", "assign_to_user": "Administrator", "assign_mode": "Assign",
		          "assignee_variable": "n2.deleted_node_value"}
		self.assertTrue(contract.reads_of("Assign to User", config), "the fixture must start broken")
		self.assertEqual(contract.reads_of("Assign to User", _stored(_node(self.workflow, "a3", "Assign to User", config))), [])

	def test_what_the_gate_lets_through_is_kept_untouched(self):
		"""A projection that dropped a live value would be worse than the bug. Pool keeps its pool."""
		stored = _stored(_node(self.workflow, "a4", "Assign to User", {
			"assignee_mode": "Pool", "assignment_rule": "Some Rule", "assign_to_user": "Administrator",
		}))
		self.assertEqual(stored.get("assignment_rule"), "Some Rule")
		self.assertNotIn("assign_to_user", stored)

	def test_an_ungated_node_is_stored_whole(self):
		"""Most fields are ungated, and the projection must be a no-op for them."""
		config = {"comment_mode": "Literal", "comment_text": "hello"}
		self.assertEqual(_stored(_node(self.workflow, "a5", "Create Note", config)), config)

	def test_it_survives_a_re_save(self):
		"""Idempotent: saving an already-projected row must not keep eroding it."""
		node = _node(self.workflow, "a6", "Create Note", {"comment_mode": "Expression",
		                                                  "comment_expression": '"hi"'})
		first = _stored(node)
		node.save(ignore_permissions=True)
		self.assertEqual(_stored(node), first)
