# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A task is held by its `assigned_to` column: a workflow-raised task on a Guest session keeps its holder, writes no ToDo and leaves the session alone.

Run:
    bench --site dev.localhost run-tests --app tatva_connect --module tatva_connect.tests.tasks.test_task_held_by_column
"""
import random

import frappe
from frappe.tests import IntegrationTestCase


class TestTaskHeldByColumn(IntegrationTestCase):
	def setUp(self):
		self.lead = frappe.get_doc({
			"doctype": "CRM Lead", "first_name": "Test", "mobile_no": f"+9190{random.randint(10_000_000, 99_999_999)}",
		}).insert(ignore_permissions=True)
		self.addCleanup(self._cleanup)

	def _cleanup(self):
		frappe.set_user("Administrator")
		frappe.db.delete("CRM Task", {"reference_docname": self.lead.name})
		frappe.delete_doc("CRM Lead", self.lead.name, force=True, ignore_permissions=True)
		frappe.db.commit()

	def test_a_workflow_raised_task_on_a_guest_session_is_held_by_its_column(self):
		frappe.set_user("Guest")
		frappe.flags.in_workflow = True
		try:
			task = frappe.get_doc({
				"doctype": "CRM Task", "title": "ZZ held-by-column probe", "status": "Todo",
				"assigned_to": "Administrator", "reference_doctype": "CRM Lead",
				"reference_docname": self.lead.name,
			}).insert(ignore_permissions=True)  # authz-ok: tier-c — test fixture, mirrors a workflow's Create Task
		finally:
			frappe.flags.in_workflow = False
		self.assertEqual(frappe.session.user, "Guest")
		self.assertEqual(frappe.db.get_value("CRM Task", task.name, "assigned_to"), "Administrator")
		self.assertFalse(frappe.db.exists("ToDo", {"reference_type": "CRM Task", "reference_name": task.name}))
