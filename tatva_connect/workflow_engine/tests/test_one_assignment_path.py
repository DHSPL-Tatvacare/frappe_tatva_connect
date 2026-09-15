# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Every assignment this app writes goes through `lead.assignment`; a caller that assigns, elevates or checks entitlement itself has grown a second path."""

import inspect
import re

from frappe.tests.utils import FrappeTestCase

from tatva_connect import bulk_actions_run
from tatva_connect.automation import actions
from tatva_connect.lead import assignment
from tatva_connect.tasks import tasks

_SECOND_PATH = re.compile(r"assign_to\.\w+\(|\.do_assignment\(|grain_entitled\(|as_workflow_operator\(")
_OWN_TODO_WRITE = re.compile(r"assign_to\.\w+\(|[\"\']doctype[\"\']\s*:\s*[\"\']ToDo[\"\']")


class TestOneAssignmentPath(FrappeTestCase):
	def test_no_verb_assigns_elevates_or_judges_entitlement_itself(self):
		found = [m.group(0) for m in _SECOND_PATH.finditer(inspect.getsource(actions))]
		self.assertEqual(found, [], "a verb reaches frappe's assignment itself — call lead.assignment instead")

	def test_every_assigning_verb_goes_through_the_one_path(self):
		for handler in (actions._action_assign_to_user, actions._action_distribute, actions._action_create_task):
			with self.subTest(handler=handler.__name__):
				self.assertRegex(inspect.getsource(handler), r"assignment\.(assign_for_workflow|draw_from_pool|assert_entitled)\(")

	def test_the_one_path_writes_through_frappe_elevated(self):
		for helper in (assignment.assign_for_workflow, assignment.draw_from_pool):
			with self.subTest(helper=helper.__name__):
				source = inspect.getsource(helper)
				self.assertIn("with as_workflow_operator():", source)
				self.assertIn("assert_entitled(", source)

	def test_bulk_actions_and_the_task_handover_write_no_assignment_of_their_own(self):
		for module in (bulk_actions_run, tasks):
			with self.subTest(module=module.__name__):
				found = [m.group(0) for m in _OWN_TODO_WRITE.finditer(inspect.getsource(module))]
				self.assertEqual(found, [], "an assignment written outside lead.assignment — call assign/unassign instead")
