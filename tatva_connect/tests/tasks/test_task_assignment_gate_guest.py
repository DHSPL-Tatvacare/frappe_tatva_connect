# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""An intake form submits as Guest. Guest holds zero permission on CRM Task (checked live on
one.tatvacare.in — `Custom DocPerm` has no row for it at all), so the moment `Task::Assignment::assignee`
went from off to on, every workflow-raised follow-up task on a Guest-born lead started dying with
`frappe.exceptions.PermissionError` inside the native `assign_to()` -> `assign()` call — proven from a
real production traceback, 7 leads on the Anaya Nivolumab flow in one day.

RED on the old code: Guest + in_workflow reproduces that exact PermissionError.
GREEN on the fix: the same call, with `frappe.flags.in_workflow` set, runs as Administrator for the
one assign call and restores the triggering session immediately after — a real person assigning a task
outside a workflow keeps their own session and its own permissions, unchanged.
"""
import random

import frappe
from frappe.tests import IntegrationTestCase

from tatva_connect.lead import assignment as assignment_module

TASK_ASSIGNEE = assignment_module.TASK_ASSIGNEE


class TestTaskAssignmentGateAsGuest(IntegrationTestCase):
	def setUp(self):
		mobile_no = f"+9190{random.randint(10_000_000, 99_999_999)}"
		self.lead = frappe.get_doc({
			"doctype": "CRM Lead", "first_name": "Test", "mobile_no": mobile_no,
		}).insert(ignore_permissions=True)
		self._before_gate = frappe.db.get_value("CRM Tatva Automation", TASK_ASSIGNEE, "enabled")
		frappe.db.set_value("CRM Tatva Automation", TASK_ASSIGNEE, "enabled", 1)
		self.addCleanup(self._cleanup)

	def _cleanup(self):
		frappe.set_user("Administrator")
		frappe.db.set_value("CRM Tatva Automation", TASK_ASSIGNEE, "enabled", self._before_gate)
		frappe.db.delete("ToDo", {"reference_type": "CRM Task", "reference_name": ["in",
			frappe.get_all("CRM Task", filters={"reference_docname": self.lead.name}, pluck="name")]})
		frappe.db.delete("CRM Task", {"reference_docname": self.lead.name})
		frappe.delete_doc("CRM Lead", self.lead.name, force=True, ignore_permissions=True)
		frappe.db.commit()

	def _insert_as_guest(self, in_workflow):
		frappe.set_user("Guest")
		frappe.flags.in_workflow = in_workflow
		try:
			return frappe.get_doc({
				"doctype": "CRM Task", "title": "ZZ guest-assign probe", "status": "Todo",
				"assigned_to": "Administrator", "reference_doctype": "CRM Lead",
				"reference_docname": self.lead.name,
			}).insert(ignore_permissions=True)  # authz-ok: tier-c — test fixture, mirrors an intake form's own insert
		finally:
			frappe.flags.in_workflow = False

	def test_workflow_triggered_guest_insert_no_longer_raises(self):
		"""RED before the fix: this raised frappe.exceptions.PermissionError, reproducing the real
		production traceback exactly."""
		task = self._insert_as_guest(in_workflow=True)
		self.assertTrue(frappe.db.exists("ToDo", {
			"reference_type": "CRM Task", "reference_name": task.name,
			"allocated_to": "Administrator", "status": "Open",
		}))

	def test_the_session_is_restored_to_guest_afterward(self):
		"""The elevation must not leak: the request that follows this insert is still Guest's own."""
		self._insert_as_guest(in_workflow=True)
		self.assertEqual(frappe.session.user, "Guest")

	def test_a_guest_insert_outside_a_workflow_still_refuses(self):
		"""Trust is scoped to the workflow engine's own claim, not handed to Guest outright. A Guest
		action that never sets in_workflow must fail exactly as it always has."""
		with self.assertRaises(frappe.PermissionError):
			self._insert_as_guest(in_workflow=False)
