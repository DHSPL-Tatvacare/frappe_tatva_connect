# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A node told where its person, pool or note comes from must name it before it publishes; one told nothing asks nothing new."""

from frappe.tests import IntegrationTestCase

from tatva_connect.workflow_engine import registry


def _required(node_type, config):
	return {p["field"] for p in registry.validate_node(node_type, config, None, mode=registry.PUBLISH) if p.get("code") == "field.required"}


class TestAChosenSourceAsksForItsValue(IntegrationTestCase):
	def test_each_source_asks_for_the_value_it_names(self):
		for node_type, config, needed in (
			("Assign to User", {"assignee_mode": "Literal", "assign_mode": "Reassign"}, "assign_to_user"),
			("Assign to User", {"assignee_mode": "From Context", "assign_mode": "Reassign"}, "assignee_variable"),
			("Assign to User", {"assignee_mode": "Pool"}, "assignment_rule"),
			("Create Task", {"assignee_mode": "Literal"}, "assign_to_user"),
			("Create Task", {"assignee_mode": "From Context"}, "assignee_variable"),
			("Create Note", {"comment_mode": "Literal"}, "comment_text"),
			("Create Note", {"comment_mode": "Expression"}, "comment_expression"),
		):
			with self.subTest(node_type=node_type, source=config):
				self.assertIn(needed, _required(node_type, config))

	def test_a_create_task_given_no_assignee_source_asks_for_no_person(self):
		self.assertFalse(_required("Create Task", {}) & {"assign_to_user", "assignee_variable"})
