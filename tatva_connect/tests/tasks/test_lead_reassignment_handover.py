# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A lead's open tasks move to its new owner the moment it's reassigned — silently, no per-task notification."""
import random
from unittest.mock import patch

import frappe
from frappe.desk.form import assign_to
from frappe.tests import IntegrationTestCase

from tatva_connect.lead import assignment as assignment_module

SECOND_USER = "zz-lead-handover@example.com"


class TestLeadReassignmentHandover(IntegrationTestCase):
	def setUp(self):
		self._tasks = []
		if not frappe.db.exists("User", SECOND_USER):
			frappe.get_doc({
				"doctype": "User", "email": SECOND_USER, "first_name": "ZZ Handover",
				"send_welcome_email": 0, "user_type": "System User",
			}).insert(ignore_permissions=True)
		mobile_no = f"+9190{random.randint(10_000_000, 99_999_999)}"
		self.lead = frappe.get_doc({
			"doctype": "CRM Lead", "first_name": "Test", "mobile_no": mobile_no,
		}).insert(ignore_permissions=True)
		assign_to.add({"doctype": "CRM Lead", "name": self.lead.name, "assign_to": ["Administrator"]})
		self.addCleanup(self._cleanup)

	def _cleanup(self):
		frappe.db.delete("ToDo", {"reference_type": "CRM Lead", "reference_name": self.lead.name})
		if self._tasks:
			frappe.db.delete("ToDo", {"reference_type": "CRM Task", "reference_name": ["in", self._tasks]})
		frappe.db.delete("CRM Task", {"reference_docname": self.lead.name})
		frappe.delete_doc("CRM Lead", self.lead.name, force=True, ignore_permissions=True)
		frappe.delete_doc("User", SECOND_USER, force=True, ignore_permissions=True)
		frappe.db.commit()

	def _task(self, status="Todo", assigned_to="Administrator"):
		task = frappe.get_doc({
			"doctype": "CRM Task", "title": "ZZ handover probe", "status": status,
			"assigned_to": assigned_to, "reference_doctype": "CRM Lead",
			"reference_docname": self.lead.name,
		}).insert(ignore_permissions=True)
		self._tasks.append(task.name)
		return task.name

	def _set_task_assignee_gate(self, enabled):
		key = assignment_module.TASK_ASSIGNEE
		before = frappe.db.get_value("CRM Tatva Automation", key, "enabled")
		frappe.db.set_value("CRM Tatva Automation", key, "enabled", enabled)
		self.addCleanup(lambda: frappe.db.set_value("CRM Tatva Automation", key, "enabled", before))

	def test_gate_off_moves_only_the_field_no_todo(self):
		"""The default in production today: Task::Assignment::assignee is off, so a MANUAL
		reassignment never creates a ToDo either — handover must match that, not outdo it."""
		self._set_task_assignee_gate(0)
		task = self._task(status="Todo", assigned_to="Administrator")

		assign_to.add({"doctype": "CRM Lead", "name": self.lead.name, "assign_to": [SECOND_USER]})

		self.assertEqual(frappe.db.get_value("CRM Task", task, "assigned_to"), SECOND_USER)
		self.assertFalse(frappe.db.exists("ToDo", {"reference_type": "CRM Task", "reference_name": task}))

	def test_gate_on_moves_the_field_and_the_todo(self):
		self._set_task_assignee_gate(1)
		task = self._task(status="Todo", assigned_to="Administrator")

		assign_to.add({"doctype": "CRM Lead", "name": self.lead.name, "assign_to": [SECOND_USER]})

		self.assertEqual(frappe.db.get_value("CRM Task", task, "assigned_to"), SECOND_USER)
		self.assertTrue(frappe.db.exists("ToDo", {
			"reference_type": "CRM Task", "reference_name": task,
			"allocated_to": SECOND_USER, "status": "Open",
		}))
		self.assertFalse(frappe.db.exists("ToDo", {
			"reference_type": "CRM Task", "reference_name": task,
			"allocated_to": "Administrator", "status": "Open",
		}))

	def test_closed_task_is_left_alone(self):
		task = self._task(status="Done", assigned_to="Administrator")

		assign_to.add({"doctype": "CRM Lead", "name": self.lead.name, "assign_to": [SECOND_USER]})

		self.assertEqual(frappe.db.get_value("CRM Task", task, "assigned_to"), "Administrator")

	def test_task_already_on_the_new_owner_is_untouched(self):
		self._set_task_assignee_gate(1)
		task = self._task(status="Todo", assigned_to=SECOND_USER)
		before = frappe.db.count("ToDo", {"reference_type": "CRM Task", "reference_name": task})

		assign_to.add({"doctype": "CRM Lead", "name": self.lead.name, "assign_to": [SECOND_USER]})

		self.assertEqual(frappe.db.count("ToDo", {"reference_type": "CRM Task", "reference_name": task}), before)

	def test_handover_never_notifies(self):
		self._task(status="Todo", assigned_to="Administrator")
		self._task(status="Todo", assigned_to="Administrator")
		before = frappe.db.count("Notification Log", {"for_user": SECOND_USER})

		assign_to.add({"doctype": "CRM Lead", "name": self.lead.name, "assign_to": [SECOND_USER]})

		# Exactly one notification — the lead's own — even though two tasks moved.
		after = frappe.db.count("Notification Log", {"for_user": SECOND_USER})
		self.assertEqual(after - before, 1)

	def test_a_failing_task_does_not_block_the_others_or_the_real_assignment(self):
		self._set_task_assignee_gate(1)
		good = self._task(status="Todo", assigned_to="Administrator")
		bad = self._task(status="Todo", assigned_to="Administrator")
		real_silent_assign = assignment_module.silent_assign

		def _flaky(doctype, name, new_owner):
			if name == bad:
				raise frappe.db.InternalError("simulated deadlock")
			return real_silent_assign(doctype, name, new_owner)

		with patch.object(assignment_module, "silent_assign", side_effect=_flaky):
			assign_to.add({"doctype": "CRM Lead", "name": self.lead.name, "assign_to": [SECOND_USER]})

		self.assertTrue(frappe.db.exists("ToDo", {
			"reference_type": "CRM Lead", "reference_name": self.lead.name,
			"allocated_to": SECOND_USER, "status": "Open",
		}))
		self.assertEqual(frappe.db.get_value("CRM Task", good, "assigned_to"), SECOND_USER)
